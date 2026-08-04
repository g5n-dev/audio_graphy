"""Textual-gradient prompt repair: diagnose a failure cluster, then edit one rule.

The loop is TextGrad's, stated in this project's terms:

1. **evaluate** -- a model reads the current rule against a cluster of reviewed
   failures and says what the rule fails to cover. That critique is the *gradient*.
2. **edit** -- a second call rewrites the rule using that critique, under explicit
   constraints. That is the *descent step*.

Why the prompts live here as Python constants rather than in ``audio_graphy/prompts``
------------------------------------------------------------------------------------
They are part of the compiler, not runtime configuration. A change to either one
changes what every future artifact means, so it has to move with ``compiler_version``
and be reviewable in the same diff as the code. ``audio_graphy/prompts/`` is a
resource directory that Docker may mount over -- a compiler whose behaviour could be
swapped by a volume mount could not be reproduced from a version string.

Why the orchestration is here and not in ``textgrad_bridge``
------------------------------------------------------------
CI never installs the ``textgrad`` extra, so anything importing the library cannot be
measured. The two calls are hidden behind :class:`GradientStep`, which the worker
binds to the real library loop and the tests bind to a stub. Everything with a
decision in it -- which clusters qualify, what counts as a usable edit, when a result
is too thin to trust -- stays on this side of that line.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from audio_graphy.optimizers.artifacts import CompiledPromptArtifact, PatchKind, PatchOrigin
from audio_graphy.optimizers.proposers import (
    METRIC_VERSION,
    PATCH_SECTION_HEADER,
    BadcaseCluster,
    ProposalRequest,
    allowed_values,
    assemble_patches,
    eligible_clusters,
    sanitize_instruction,
    template_patch_body,
)

logger = logging.getLogger(__name__)

TEXTGRAD_COMPILER: PatchOrigin = "textgrad_tgd"
TEXTGRAD_COMPILER_VERSION = "textgrad-tgd-v1"

#: Below this a cluster's measured effect is noise. The patch is still produced --
#: a reviewer may recognise the failure from three examples -- but the record says so
#: and the UI repeats it, because "improved 2 of 3" reads like evidence and is not.
LOW_CONFIDENCE_SUPPORT = 10

#: The critique call. Deliberately asks for a *deficiency*, not a rewrite: letting one
#: call do both produces a rewrite with a rationalisation attached, and the rationale
#: is then untestable against the failure it claims to explain.
EVALUATION_SYSTEM = (
    "你是对话打标系统的错误分析师。给定一条现行判定规则和一组已由人工复核的失败样本，"
    "指出这条规则**为什么**没能覆盖这些样本。要求：\n"
    "1. 只诊断，不要给出改写后的规则；\n"
    "2. 结论必须能由给出的复核事实支撑，不要臆测模型内部原因；\n"
    "3. 如果失败源于转写质量或标签定义本身而非规则表述，直接说明，不要硬找规则的毛病；\n"
    "4. 控制在 150 字以内。"
)

#: The descent call. The constraint list is passed to the optimizer separately as well
#: -- TextGrad injects them into its own template -- but stating them here keeps the
#: native and library paths saying the same thing.
TGD_SYSTEM = (
    "你是提示词工程师。根据给出的诊断，改写这条标签判定规则。要求：\n"
    "1. 只输出改写后的规则正文，不要解释、不要编号、不要引号或代码块；\n"
    "2. 必须点名涉及的标签 key；\n"
    "3. 不得鼓励在缺乏文本依据时猜测——没有依据就省略该标签，这是不可推翻的前提；\n"
    "4. 保留原规则中仍然正确的部分，只修补诊断指出的缺口；\n"
    "5. 控制在 120 字以内。"
)

#: Handed to ``TextualGradientDescent(constraints=...)`` verbatim.
TGD_CONSTRAINTS: tuple[str, ...] = (
    "只输出一条规则，不要罗列多个候选。",
    "必须出现该规则涉及的标签 key。",
    "不得鼓励在缺乏直接文本依据时输出标签。",
    "不得引入新的标签取值或新的输出字段。",
)


class GradientError(RuntimeError):
    """Raised when a gradient step cannot produce a reviewable edit."""


@dataclass(frozen=True, slots=True)
class GradientOutcome:
    """One completed evaluate-then-edit round for a single cluster."""

    gradient_text: str
    proposed_edit: str
    rounds: int = 1


class GradientStep(Protocol):
    """Run the evaluate-then-edit loop for one cluster.

    Bound by the worker to the real ``textgrad`` loop. Synchronous because both
    TextGrad and DSPy are; the worker calls it from a thread.
    """

    def run(
        self,
        *,
        current_rule: str,
        evaluation_prompt: str,
        role_description: str,
        iterations: int,
    ) -> GradientOutcome: ...


def build_evaluation_prompt(
    cluster: BadcaseCluster,
    definition: Mapping[str, Any] | None,
    *,
    baseline_prompt: str,
) -> str:
    """State the failure in facts a reviewer already signed off on.

    Occurrence counts and review reason codes are records. "The model missed this
    because customers phrase discounts indirectly" is a story, and inviting the
    critique model to invent one would put that story in ``gradient_text``, where it
    reads to a reviewer exactly like a finding.
    """

    lines = [
        # EVALUATION_SYSTEM is prepended rather than left to the engine's class-level
        # default: TextLoss passes this whole string as the system prompt, so a
        # constant that only filled in when no system prompt was given would never
        # actually constrain anything.
        EVALUATION_SYSTEM,
        "",
        "现行判定规则（节选）：",
        baseline_prompt.strip()[:600] or "（无）",
        "",
        "已复核的失败样本聚类：",
        f"- 标签 key：{cluster.tag_key}",
        f"- 失败环节：{cluster.failure_stage}",
        f"- 复核理由：{cluster.reason_code}",
        f"- 真值状态：{cluster.truth_state}",
        f"- 样本数：{cluster.occurrence_count}",
    ]
    values = allowed_values(definition)
    if values:
        lines.append(f"- 该标签合法取值：{'、'.join(values)}")
    if cluster.occurrence_count < LOW_CONFIDENCE_SUPPORT:
        # Told to the model as well as recorded: a confident-sounding diagnosis drawn
        # from four samples is the failure mode worth pre-empting.
        lines.append("- 注意：样本量偏小，请相应保留判断。")
    lines.extend(["", "请指出现行规则未能覆盖这些样本的原因："])
    return "\n".join(lines)


def tag_key_deltas(
    *,
    before: Mapping[str, float],
    after: Mapping[str, float],
) -> dict[str, float]:
    """Per-tag change between two replays, over the union of both tag sets.

    A tag present in only one side counts as moving from or to zero rather than being
    dropped: a patch that makes the tagger stop emitting a label entirely is the most
    important side effect there is, and silently omitting it would hide exactly that.
    """

    keys = set(before) | set(after)
    deltas = {
        key: round(float(after.get(key, 0.0)) - float(before.get(key, 0.0)), 6) for key in keys
    }
    return {key: value for key, value in sorted(deltas.items()) if value != 0.0}


def build_evaluation_record(
    cluster: BadcaseCluster,
    *,
    rounds: int,
    replay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The fourth panel of the UI card: what is actually known about the effect.

    When no replay has happened this says so. It does not emit a zeroed F1 -- the
    panel renders any numeric field it is given, and a fabricated zero is
    indistinguishable from a measured regression.
    """

    record: dict[str, Any] = {
        "source_badcase_count": len(cluster.badcase_ids),
        "cluster_support": cluster.occurrence_count,
        "gradient_rounds": rounds,
        "low_confidence": cluster.occurrence_count < LOW_CONFIDENCE_SUPPORT,
        "replayed": False,
    }
    if replay is None:
        return record

    record["replayed"] = True
    before = replay.get("baseline_label_f1")
    after = replay.get("candidate_label_f1")
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        record["tag_key_deltas"] = tag_key_deltas(before=before, after=after)
    for key in ("macro_f1_delta", "resolved_badcases", "regressed_tag_keys"):
        if key in replay:
            record[key] = replay[key]
    return record


@dataclass(frozen=True, slots=True)
class TextGradProposer:
    """Repair one rule per failure cluster with a textual-gradient step.

    ``step`` is injected: the worker binds the real TextGrad loop, tests bind a stub.
    An edit that fails validation degrades to the deterministic template rule, for the
    same reason the grounded proposer does -- a rule that contradicts the baseline
    contract is worse than a dull one.
    """

    step: GradientStep
    iterations: int = 2
    compiler: str = TEXTGRAD_COMPILER
    compiler_version: str = TEXTGRAD_COMPILER_VERSION
    gradients: dict[str, dict[str, Any]] = field(default_factory=dict, compare=False)

    def propose(self, request: ProposalRequest) -> CompiledPromptArtifact:
        entries: list[tuple[BadcaseCluster, str, PatchKind, str]] = []
        # Keyed by cluster, not by body: two clusters can reach the same rule, and
        # keying by text would let the second one overwrite the first one's diagnosis.
        diagnoses: dict[str, str] = {}
        rounds: dict[str, int] = {}
        for cluster in eligible_clusters(request):
            definition = request.definitions.get(cluster.tag_key)
            fallback, kind = template_patch_body(cluster, definition)
            body, note, diagnosis, ran = self._repair(
                cluster,
                definition,
                baseline_prompt=request.baseline_prompt,
                fallback=fallback,
            )
            entries.append((cluster, body, kind, note))
            diagnoses[cluster.cluster_key] = diagnosis
            rounds[cluster.cluster_key] = ran

        patches, grouping = assemble_patches(entries, origin=TEXTGRAD_COMPILER)
        self.gradients.clear()
        for patch in patches:
            clusters = grouping[patch.patch_id]
            self.gradients[patch.patch_id] = _merge_records(
                clusters,
                diagnoses=diagnoses,
                rounds=rounds,
            )

        return CompiledPromptArtifact(
            baseline_prompt=request.baseline_prompt,
            header=(
                f"{request.baseline_prompt.strip()}\n\n{PATCH_SECTION_HEADER}"
                if request.baseline_prompt.strip()
                else PATCH_SECTION_HEADER
            )
            if patches
            else request.baseline_prompt.strip(),
            compiler=TEXTGRAD_COMPILER,
            compiler_version=self.compiler_version,
            metric_version=METRIC_VERSION,
            patches=patches,
            demos=(),
            accepted_patch_ids=frozenset(patch.patch_id for patch in patches),
        )

    def _repair(
        self,
        cluster: BadcaseCluster,
        definition: Mapping[str, Any] | None,
        *,
        baseline_prompt: str,
        fallback: str,
    ) -> tuple[str, str, str, int]:
        """Return ``(body, note, diagnosis, rounds_run)`` for one cluster."""

        prompt = build_evaluation_prompt(cluster, definition, baseline_prompt=baseline_prompt)
        try:
            outcome = self.step.run(
                current_rule=fallback,
                evaluation_prompt=prompt,
                role_description=f"标签 {cluster.tag_key} 的判定规则",
                iterations=self.iterations,
            )
        except Exception:
            logger.warning(
                "textgrad step failed for cluster=%s, using the template rule",
                cluster.cluster_key,
                exc_info=True,
            )
            return fallback, "梯度步骤失败，已回落到模板规则。", "", 0

        body = sanitize_instruction(outcome.proposed_edit, tag_key=cluster.tag_key)
        if body is None:
            logger.info(
                "textgrad edit rejected for cluster=%s, using the template rule",
                cluster.cluster_key,
            )
            # The diagnosis survives the rejected edit: it is what a reviewer reads to
            # decide whether falling back to the template was the right call.
            return (
                fallback,
                "改写未通过校验，已回落到模板规则。",
                outcome.gradient_text,
                outcome.rounds,
            )
        return (
            body,
            "由梯度步骤依据该聚类的复核结论改写。",
            outcome.gradient_text,
            outcome.rounds,
        )


def _merge_records(
    clusters: Sequence[BadcaseCluster],
    *,
    diagnoses: Mapping[str, str],
    rounds: Mapping[str, int],
) -> dict[str, Any]:
    """Fold every cluster behind one patch into a single evidence record."""

    merged = build_evaluation_record(
        clusters[0],
        rounds=max((rounds.get(c.cluster_key, 0) for c in clusters), default=0),
    )
    merged["source_badcase_count"] = len({bid for c in clusters for bid in c.badcase_ids})
    merged["cluster_support"] = sum(c.occurrence_count for c in clusters)
    merged["low_confidence"] = merged["cluster_support"] < LOW_CONFIDENCE_SUPPORT
    texts = [diagnoses.get(c.cluster_key, "") for c in clusters]
    merged["gradient_text"] = "\n".join(text for text in texts if text)
    return merged


def gradient_rows(
    artifact: CompiledPromptArtifact,
    records: Mapping[str, Mapping[str, Any]],
    clusters_by_patch: Mapping[str, Sequence[BadcaseCluster]],
) -> list[dict[str, Any]]:
    """Shape the per-patch records the way the prompt-lab service stores them."""

    rows: list[dict[str, Any]] = []
    for patch in artifact.patches:
        record = dict(records.get(patch.patch_id) or {})
        clusters = clusters_by_patch.get(patch.patch_id) or ()
        # An empty diagnosis means the step never ran; the rationale is then the only
        # honest thing to show, and it says the fallback was used.
        gradient_text = str(record.pop("gradient_text", "") or "") or patch.rationale
        rows.append(
            {
                "patch_id": patch.patch_id,
                "tag_key": patch.target_tag_keys[0] if patch.target_tag_keys else None,
                "failure_stage": clusters[0].failure_stage if clusters else "tag_reasoning",
                "gradient_text": gradient_text,
                "proposed_edit": patch.body,
                "evaluation": record,
                "source_badcase_id": (
                    patch.source_badcase_ids[0] if patch.source_badcase_ids else None
                ),
            }
        )
    return rows


__all__ = [
    "EVALUATION_SYSTEM",
    "LOW_CONFIDENCE_SUPPORT",
    "TEXTGRAD_COMPILER",
    "TEXTGRAD_COMPILER_VERSION",
    "TGD_CONSTRAINTS",
    "TGD_SYSTEM",
    "GradientError",
    "GradientOutcome",
    "GradientStep",
    "TextGradProposer",
    "build_evaluation_prompt",
    "build_evaluation_record",
    "gradient_rows",
    "tag_key_deltas",
]
