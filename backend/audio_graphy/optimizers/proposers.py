"""Turn reviewed failures into prompt patches.

The proposer here is deliberately dumb: it clusters badcases, counts them, and emits
a fixed corrective sentence per failure shape. It cannot invent an insight the data
does not already contain, which is exactly why it is safe to run without a model --
and why it is the right baseline to compare a DSPy or TextGrad proposer against.

Two rules constrain every template:

* Never contradict the stable contract in ``TagExtractor._system_prompt``. That
  contract already says unsupported labels must be omitted; a patch that nudges
  toward guessing would cancel it out and the net effect would be noise.
* Never assert a specific cause. A count and a review reason are facts; "the model
  missed this because customers phrase discounts indirectly" is a story, and a
  proposer with no language model has no business telling it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from audio_graphy.optimizers.artifacts import (
    CompiledPromptArtifact,
    PatchKind,
    PatchOrigin,
    PromptPatch,
    build_patch_id,
)

logger = logging.getLogger(__name__)

BUILTIN_COMPILER_VERSION = "builtin-proposer-v1"
METRIC_VERSION = "prompt-lab-metric-v1"

#: Compilers this build can actually run.
#:
#: ``CompilerName`` in the API schema is deliberately wider: it names every compiler
#: the design covers, so the public contract does not churn as each one lands. That
#: gap has to be refused loudly. Quietly falling back to ``builtin`` would hand the
#: user an artifact from a compiler they did not ask for, labelled with a
#: ``compiler`` field that says otherwise -- and every downstream comparison between
#: compilers would then be reading fabricated data.
IMPLEMENTED_COMPILERS: frozenset[str] = frozenset({"builtin", "builtin_grounded", "textgrad_tgd"})


class UnsupportedCompilerError(ValueError):
    """Raised when a request names a compiler this build cannot run."""


def assert_compiler_supported(name: str) -> None:
    """Raise :class:`UnsupportedCompilerError` unless *name* has an implementation."""

    if name not in IMPLEMENTED_COMPILERS:
        supported = "、".join(sorted(IMPLEMENTED_COMPILERS))
        raise UnsupportedCompilerError(f"编译器 {name} 在当前版本尚未实现，可用的是：{supported}")


_DEFAULT_MAX_PATCHES = 8
_DEFAULT_MIN_CLUSTER_SUPPORT = 3

# Failures the prompt cannot fix: the transcript itself was wrong before the tagger
# ever saw it. Mirrors _UPSTREAM_FAILURE_STAGES in the governance service.
_UPSTREAM_STAGES = frozenset({"vad", "asr", "speaker", "boundary", "insufficient_audio"})

PATCH_SECTION_HEADER = "以下规则由历史复核结论归纳，用于补充标签判定标准。"


@dataclass(frozen=True, slots=True)
class BadcaseCluster:
    """A group of reviewed failures that share a tag, a stage and a review reason."""

    cluster_key: str
    tag_key: str
    failure_stage: str
    reason_code: str
    truth_state: str
    occurrence_count: int
    badcase_ids: tuple[int, ...]

    @property
    def support(self) -> int:
        return self.occurrence_count


@dataclass(frozen=True, slots=True)
class ProposalRequest:
    baseline_prompt: str
    clusters: tuple[BadcaseCluster, ...]
    definitions: Mapping[str, Mapping[str, Any]]
    max_patches: int = _DEFAULT_MAX_PATCHES
    min_cluster_support: int = _DEFAULT_MIN_CLUSTER_SUPPORT


class PromptProposer(Protocol):
    """Anything that can turn reviewed failures into a reviewable prompt candidate.

    The two names are read-only properties rather than plain attributes: a plain
    attribute in a Protocol means *settable*, which no frozen implementation can
    satisfy -- and a proposer that could be renamed after construction would let an
    artifact claim it came from a compiler that never ran.
    """

    @property
    def compiler(self) -> str: ...

    @property
    def compiler_version(self) -> str: ...

    def propose(self, request: ProposalRequest) -> CompiledPromptArtifact: ...


def cluster_badcases(rows: Sequence[Mapping[str, Any]]) -> tuple[BadcaseCluster, ...]:
    """Group badcase rows into deterministic clusters, dropping upstream failures.

    Accepts plain mappings rather than ORM rows so the clustering rules stay testable
    without a database.
    """

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        stage = str(row.get("failure_stage") or "")
        root_cause = row.get("root_cause")
        root = root_cause if isinstance(root_cause, Mapping) else {}
        if stage in _UPSTREAM_STAGES or root.get("upstream_routed") is True:
            continue
        tag_key = str(row.get("tag_key") or "")
        if not tag_key:
            continue
        reason_code = str(root.get("reason_code") or "review_feedback")
        key = str(row.get("cluster_key") or f"{stage}:{tag_key}:{reason_code}")
        badcase_id = row.get("id")
        bucket = grouped.setdefault(
            key,
            {
                "tag_key": tag_key,
                "failure_stage": stage,
                "reason_code": reason_code,
                "truth_state": str(root.get("truth_state") or "present"),
                "occurrence_count": 0,
                "badcase_ids": set(),
            },
        )
        bucket["occurrence_count"] += max(1, int(row.get("occurrence_count") or 1))
        if isinstance(badcase_id, int):
            bucket["badcase_ids"].add(badcase_id)
    return tuple(
        sorted(
            (
                BadcaseCluster(
                    cluster_key=key,
                    tag_key=str(bucket["tag_key"]),
                    failure_stage=str(bucket["failure_stage"]),
                    reason_code=str(bucket["reason_code"]),
                    truth_state=str(bucket["truth_state"]),
                    occurrence_count=int(bucket["occurrence_count"]),
                    badcase_ids=tuple(sorted(bucket["badcase_ids"])),
                )
                for key, bucket in grouped.items()
            ),
            key=lambda cluster: (-cluster.occurrence_count, cluster.cluster_key),
        )
    )


def allowed_values(definition: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(definition, Mapping):
        return []
    raw = definition.get("allowed_values")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    return [str(value) for value in raw]


def template_patch_body(
    cluster: BadcaseCluster,
    definition: Mapping[str, Any] | None,
) -> tuple[str, PatchKind]:
    """Return (body, kind) for a cluster. Body states a count, then one instruction."""

    tag = cluster.tag_key
    count = cluster.occurrence_count
    if cluster.failure_stage == "evidence":
        return (
            f"标签「{tag}」在 {count} 个已复核样本上证据引用不当。"
            f"输出该标签时，evidence_segment_ids 必须且只能包含直接支持该判定的 segment。",
            "constraint_add",
        )
    if cluster.failure_stage == "schema":
        values = allowed_values(definition)
        suffix = f"该标签的合法取值仅为：{'、'.join(values)}。" if values else ""
        return (
            f"标签「{tag}」在 {count} 个已复核样本上违反了输出格式。{suffix}"
            f"请严格按 response schema 输出，不要新增字段或改写取值。",
            "constraint_add",
        )
    if cluster.truth_state in {"absent", "not_applicable"}:
        return (
            f"标签「{tag}」在 {count} 个已复核样本上被误判为成立。"
            f"仅当 segment 中存在直接文本依据时才输出该标签；推测、暗示或上下文联想一律省略。",
            "rule_clarification",
        )
    values = allowed_values(definition)
    if values:
        return (
            f"标签「{tag}」在 {count} 个已复核样本上取值判错。"
            f"可选值仅为：{'、'.join(values)}，请逐一比对后选择最贴合文本证据的一项。",
            "rule_clarification",
        )
    return (
        f"标签「{tag}」在 {count} 个已复核样本上被漏判。"
        f"若某个 segment 明确支持该标签，即使表述间接也应输出并引用该 segment。",
        "rule_clarification",
    )


def eligible_clusters(request: ProposalRequest) -> list[BadcaseCluster]:
    eligible = [
        cluster for cluster in request.clusters if cluster.support >= request.min_cluster_support
    ]
    return sorted(
        eligible,
        key=lambda cluster: (-cluster.occurrence_count, cluster.cluster_key),
    )[: max(0, request.max_patches)]


def assemble_patches(
    entries: Sequence[tuple[BadcaseCluster, str, PatchKind, str]],
    *,
    origin: PatchOrigin,
) -> tuple[tuple[PromptPatch, ...], dict[str, tuple[BadcaseCluster, ...]]]:
    """Number the patches, merging any two that arrived at the same advice.

    ``build_patch_id`` is a content address by design -- identical advice is meant to
    keep its identity across compiles. Two clusters can reach the same sentence
    (same tag, same failure shape, same count), and emitting both would abort the
    whole compile on the uniqueness check in ``CompiledPromptArtifact``. Merging is
    also what a reviewer wants: one rule, listing every cluster that motivated it.
    """

    merged: dict[str, dict[str, Any]] = {}
    for cluster, body, kind, note in entries:
        patch_id = build_patch_id(origin=origin, body=body, target_tag_keys=[cluster.tag_key])
        bucket = merged.get(patch_id)
        if bucket is None:
            merged[patch_id] = {
                "kind": kind,
                "body": body,
                "clusters": [cluster],
                "note": note,
                "badcase_ids": set(cluster.badcase_ids),
            }
            continue
        bucket["clusters"].append(cluster)
        bucket["badcase_ids"].update(cluster.badcase_ids)

    patches: list[PromptPatch] = []
    for ordinal, (patch_id, bucket) in enumerate(merged.items(), start=1):
        clusters: list[BadcaseCluster] = bucket["clusters"]
        occurrences = sum(cluster.occurrence_count for cluster in clusters)
        keys = "、".join(cluster.cluster_key for cluster in clusters)
        patches.append(
            PromptPatch(
                patch_id=patch_id,
                kind=bucket["kind"],
                origin=origin,
                ordinal=ordinal,
                body=bucket["body"],
                rationale=(
                    f"聚类 {keys} 共 {occurrences} 例，"
                    f"复核理由 {clusters[0].reason_code}。{bucket['note']}"
                ),
                target_tag_keys=(clusters[0].tag_key,),
                source_badcase_ids=tuple(sorted(bucket["badcase_ids"])),
            )
        )
    grouping = {patch_id: tuple(bucket["clusters"]) for patch_id, bucket in merged.items()}
    return tuple(patches), grouping


@dataclass(frozen=True, slots=True)
class BuiltinProposer:
    """A deterministic, model-free proposer. No extras, no provider calls."""

    compiler: str = "builtin"
    compiler_version: str = BUILTIN_COMPILER_VERSION

    def propose(self, request: ProposalRequest) -> CompiledPromptArtifact:
        entries: list[tuple[BadcaseCluster, str, PatchKind, str]] = []
        for cluster in eligible_clusters(request):
            body, kind = template_patch_body(cluster, request.definitions.get(cluster.tag_key))
            entries.append((cluster, body, kind, ""))
        patches, _ = assemble_patches(entries, origin="builtin")

        return CompiledPromptArtifact(
            baseline_prompt=request.baseline_prompt,
            # The baseline policy is kept verbatim: this proposer adds constraints, it
            # never rewrites what a human already approved.
            header=(
                f"{request.baseline_prompt.strip()}\n\n{PATCH_SECTION_HEADER}"
                if request.baseline_prompt.strip()
                else PATCH_SECTION_HEADER
            )
            if patches
            else request.baseline_prompt.strip(),
            compiler="builtin",
            compiler_version=self.compiler_version,
            metric_version=METRIC_VERSION,
            patches=patches,
            demos=(),
            accepted_patch_ids=frozenset(patch.patch_id for patch in patches),
        )


GROUNDED_COMPILER: PatchOrigin = "builtin_grounded"
GROUNDED_COMPILER_VERSION = "builtin-grounded-v1"

#: One call per eligible cluster, and the cluster list is already capped by
#: ``max_patches`` (≤32, default 8). The proposer therefore cannot outspend the
#: compile budget by looping -- there is no loop.
_GROUNDED_SYSTEM = (
    "你是提示词工程师，负责为对话打标模型补充判定规则。\n"
    "给定一类反复出现的判定错误，写出一条中文规则来纠正它。要求：\n"
    "1. 只写规则本身，不要解释、不要编号、不要引号或代码块；\n"
    "2. 必须点名涉及的标签 key；\n"
    "3. 不得鼓励在缺乏文本依据时猜测——没有依据就省略该标签，这是不可推翻的前提；\n"
    "4. 不要臆断错误成因，只依据给出的复核结论；\n"
    "5. 控制在 120 字以内。"
)

#: Phrases that would cancel out the baseline contract. The system prompt already
#: says "omit when unsupported"; a proposed rule that nudges toward guessing would
#: leave the two halves of the prompt contradicting each other, and the net effect on
#: a metric is noise rather than the regression it looks like.
_CONTRADICTIONS: tuple[str, ...] = (
    "猜测",
    "推测",
    "即使没有证据",
    "无需证据",
    "可以不引用",
    "尽量都输出",
    "宁可多标",
)

_MAX_GROUNDED_BODY_CHARS = 200


class InstructionWriter(Protocol):
    """Synchronous single-turn completion. Bound to a ``GatewayLM`` by the worker."""

    def complete_text(self, prompt: str, *, system: str | None = None) -> str: ...


def _grounded_prompt(
    cluster: BadcaseCluster,
    definition: Mapping[str, Any] | None,
    *,
    baseline_prompt: str,
) -> str:
    values = allowed_values(definition)
    lines = [
        "当前判定规则（节选）：",
        baseline_prompt.strip()[:600] or "（无）",
        "",
        "需要纠正的错误类别：",
        f"- 标签 key：{cluster.tag_key}",
        f"- 失败环节：{cluster.failure_stage}",
        f"- 复核理由：{cluster.reason_code}",
        f"- 真值状态：{cluster.truth_state}",
        f"- 已复核样本数：{cluster.occurrence_count}",
    ]
    if values:
        lines.append(f"- 该标签合法取值：{'、'.join(values)}")
    lines.extend(["", "请写出这一条规则："])
    return "\n".join(lines)


def sanitize_instruction(raw: str, *, tag_key: str) -> str | None:
    """Return a usable rule body, or ``None`` if the model's output cannot be trusted.

    Rejecting is not a failure mode to avoid -- the caller falls back to the
    deterministic template, which is always correct if duller. Shipping a rule that
    contradicts the baseline contract would be worse than shipping no rule.
    """

    body = raw.strip().strip("`").strip()
    # Models reach for fenced blocks and leading bullets even when told not to.
    body = body.removeprefix("```").removesuffix("```").strip()
    body = body.lstrip("-*0123456789. 、").strip().strip('"').strip("“”").strip()
    if not body:
        return None
    if len(body) > _MAX_GROUNDED_BODY_CHARS:
        return None
    if tag_key not in body:
        # An instruction that never names its tag cannot be attributed to one, and the
        # diff view would show an edit nobody can tie back to a failure.
        return None
    if any(phrase in body for phrase in _CONTRADICTIONS):
        return None
    return body


@dataclass(frozen=True, slots=True)
class BuiltinGroundedProposer:
    """Grounded instruction proposal without DSPy.

    Mirrors what ``dspy.propose.GroundedProposer`` does -- condition a strong model on
    the current instruction plus observed failures, and ask for a corrective rule --
    but owns its own meta-prompt, so DSPy stays an optional enhancement rather than a
    hard dependency. Anything the model returns that fails validation degrades to the
    template body ``BuiltinProposer`` would have produced.
    """

    writer: InstructionWriter
    compiler: str = GROUNDED_COMPILER
    compiler_version: str = GROUNDED_COMPILER_VERSION

    def propose(self, request: ProposalRequest) -> CompiledPromptArtifact:
        entries: list[tuple[BadcaseCluster, str, PatchKind, str]] = []
        for cluster in eligible_clusters(request):
            definition = request.definitions.get(cluster.tag_key)
            template_body, kind = template_patch_body(cluster, definition)
            body, origin_note = self._body_for(
                cluster,
                definition,
                baseline_prompt=request.baseline_prompt,
                fallback=template_body,
            )
            entries.append((cluster, body, kind, origin_note))
        patches, _ = assemble_patches(entries, origin=GROUNDED_COMPILER)

        return CompiledPromptArtifact(
            baseline_prompt=request.baseline_prompt,
            header=(
                f"{request.baseline_prompt.strip()}\n\n{PATCH_SECTION_HEADER}"
                if request.baseline_prompt.strip()
                else PATCH_SECTION_HEADER
            )
            if patches
            else request.baseline_prompt.strip(),
            compiler=GROUNDED_COMPILER,
            compiler_version=self.compiler_version,
            metric_version=METRIC_VERSION,
            patches=patches,
            demos=(),
            accepted_patch_ids=frozenset(patch.patch_id for patch in patches),
        )

    def _body_for(
        self,
        cluster: BadcaseCluster,
        definition: Mapping[str, Any] | None,
        *,
        baseline_prompt: str,
        fallback: str,
    ) -> tuple[str, str]:
        try:
            raw = self.writer.complete_text(
                _grounded_prompt(cluster, definition, baseline_prompt=baseline_prompt),
                system=_GROUNDED_SYSTEM,
            )
        except Exception:
            # A provider failure must not lose the cluster: the template rule is still
            # a correct statement about the evidence.
            logger.warning(
                "grounded proposal failed for cluster=%s, using the template rule",
                cluster.cluster_key,
                exc_info=True,
            )
            return fallback, "模型提案失败，已回落到模板规则。"

        body = sanitize_instruction(raw, tag_key=cluster.tag_key)
        if body is None:
            logger.info(
                "grounded proposal rejected for cluster=%s, using the template rule",
                cluster.cluster_key,
            )
            return fallback, "模型提案未通过校验，已回落到模板规则。"
        return body, "由模型基于该聚类的复核结论生成。"
