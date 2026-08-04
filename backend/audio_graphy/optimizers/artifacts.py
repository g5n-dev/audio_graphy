"""Deterministic assembly of a compiled prompt from individually reviewable parts.

A compiled prompt is not a blob. It is a header plus an ordered set of patches plus
an ordered set of demonstrations, each of which a human can accept or reject on its
own. Everything in this module is a pure function of those parts, which is what makes
partial acceptance safe: the same accepted set always renders byte-identical text, so
the resulting checksum -- and therefore the TaggerVersion it materializes into -- is
idempotent no matter how many times a reviewer submits.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from audio_graphy.core.canonical import canonical_checksum, estimate_prompt_tokens

PatchKind = Literal[
    "instruction_rewrite",
    "constraint_add",
    "rule_clarification",
]
PatchOrigin = Literal[
    "builtin",
    "builtin_grounded",
    "dspy_mipro",
    "dspy_bootstrap",
    "dspy_gepa",
    "textgrad_tgd",
    "manual",
]
RedactionMode = Literal["verbatim", "masked", "synthetic"]

_DEMO_SECTION_HEADING = "示例："
_SECTION_SEPARATOR = "\n\n"


class PromptArtifactError(ValueError):
    """Raised when an artifact cannot be assembled deterministically."""


@dataclass(frozen=True, slots=True)
class PromptPatch:
    """One reviewable edit to the tag policy section of a prompt."""

    patch_id: str
    kind: PatchKind
    origin: PatchOrigin
    ordinal: int
    body: str
    rationale: str
    target_tag_keys: tuple[str, ...] = ()
    gradient_text: str | None = None
    source_badcase_ids: tuple[int, ...] = ()
    source_gold_label_ids: tuple[int, ...] = ()

    @property
    def prompt_token_estimate(self) -> int:
        return estimate_prompt_tokens(self.body)

    def as_payload(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "kind": self.kind,
            "origin": self.origin,
            "ordinal": self.ordinal,
            "body": self.body,
            "rationale": self.rationale,
            "target_tag_keys": list(self.target_tag_keys),
            "gradient_text": self.gradient_text,
            "source_badcase_ids": list(self.source_badcase_ids),
            "source_gold_label_ids": list(self.source_gold_label_ids),
        }


@dataclass(frozen=True, slots=True)
class PromptDemo:
    """A worked example inlined into the prompt, plus where it came from.

    ``source_checksum`` fingerprints the pre-redaction snapshot so an erasure request
    can find every artifact a subject ever reached without storing the subject again.
    """

    demo_id: str
    gold_label_id: int
    subject_type: str
    subject_id: int
    rendered_text: str
    redaction_mode: RedactionMode
    source_checksum: str
    reception_id: int | None = None
    segment_ids: tuple[int, ...] = ()
    recording_ids: tuple[int, ...] = ()

    @property
    def prompt_token_estimate(self) -> int:
        return estimate_prompt_tokens(self.rendered_text)

    def as_payload(self) -> dict[str, Any]:
        return {
            "demo_id": self.demo_id,
            "gold_label_id": self.gold_label_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "rendered_text": self.rendered_text,
            "redaction_mode": self.redaction_mode,
            "source_checksum": self.source_checksum,
            "reception_id": self.reception_id,
            "segment_ids": list(self.segment_ids),
            "recording_ids": list(self.recording_ids),
        }


@dataclass(frozen=True, slots=True)
class CompiledPromptArtifact:
    """A prompt candidate whose every part can be accepted or rejected separately."""

    baseline_prompt: str
    header: str
    compiler: PatchOrigin
    compiler_version: str
    metric_version: str
    patches: tuple[PromptPatch, ...] = ()
    demos: tuple[PromptDemo, ...] = ()
    accepted_patch_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        patch_ids = [patch.patch_id for patch in self.patches]
        if len(set(patch_ids)) != len(patch_ids):
            raise PromptArtifactError("patch_id must be unique within an artifact")
        demo_ids = [demo.demo_id for demo in self.demos]
        if len(set(demo_ids)) != len(demo_ids):
            raise PromptArtifactError("demo_id must be unique within an artifact")
        unknown = self.accepted_patch_ids - set(patch_ids)
        if unknown:
            raise PromptArtifactError(
                "accepted_patch_ids reference unknown patches: " + ", ".join(sorted(unknown))
            )

    @property
    def accepted_patches(self) -> tuple[PromptPatch, ...]:
        """Accepted patches in render order: by ordinal, ties broken by id."""

        return tuple(
            sorted(
                (patch for patch in self.patches if patch.patch_id in self.accepted_patch_ids),
                key=lambda patch: (patch.ordinal, patch.patch_id),
            )
        )

    def render(self) -> str:
        """Assemble the tag policy section that becomes ``prompt_content``.

        Deterministic by construction: the output depends only on the header, the
        accepted patches in ordinal order, and the surviving demos in order.
        """

        blocks: list[str] = []
        header = self.header.strip()
        if header:
            blocks.append(header)
        blocks.extend(body for patch in self.accepted_patches if (body := patch.body.strip()))
        demo_blocks = [text for demo in self.demos if (text := demo.rendered_text.strip())]
        if demo_blocks:
            blocks.append(_DEMO_SECTION_HEADING)
            blocks.extend(demo_blocks)
        return _SECTION_SEPARATOR.join(blocks)

    @property
    def prompt_token_estimate(self) -> int:
        return estimate_prompt_tokens(self.render())

    def as_payload(self) -> dict[str, Any]:
        return {
            "baseline_prompt": self.baseline_prompt,
            "header": self.header,
            "compiler": self.compiler,
            "compiler_version": self.compiler_version,
            "metric_version": self.metric_version,
            "patches": [patch.as_payload() for patch in self.patches],
            "demos": [demo.as_payload() for demo in self.demos],
            "accepted_patch_ids": sorted(self.accepted_patch_ids),
            "rendered_prompt": self.render(),
        }

    def checksum(self) -> str:
        """Identity of this artifact, stable across equivalent reviewer sessions."""

        return canonical_checksum(self.as_payload())


def rematerialize(
    artifact: CompiledPromptArtifact,
    *,
    accepted_patch_ids: Collection[str],
    dropped_demo_ids: Collection[str] = (),
) -> CompiledPromptArtifact:
    """Apply a reviewer's accept/reject decisions and return the resulting artifact.

    Rejected patches and dropped demos are removed rather than flagged, so the child
    artifact renders exactly what will be served. Unknown ids are an error: they mean
    a reviewer acted on a stale view, and silently ignoring them would materialize a
    prompt nobody actually approved.
    """

    accepted = frozenset(accepted_patch_ids)
    dropped = frozenset(dropped_demo_ids)
    known_patches = {patch.patch_id for patch in artifact.patches}
    known_demos = {demo.demo_id for demo in artifact.demos}
    if unknown_patches := accepted - known_patches:
        raise PromptArtifactError(
            "unknown patch_id in decisions: " + ", ".join(sorted(unknown_patches))
        )
    if unknown_demos := dropped - known_demos:
        raise PromptArtifactError(
            "unknown demo_id in decisions: " + ", ".join(sorted(unknown_demos))
        )
    return CompiledPromptArtifact(
        baseline_prompt=artifact.baseline_prompt,
        header=artifact.header,
        compiler=artifact.compiler,
        compiler_version=artifact.compiler_version,
        metric_version=artifact.metric_version,
        patches=tuple(patch for patch in artifact.patches if patch.patch_id in accepted),
        demos=tuple(demo for demo in artifact.demos if demo.demo_id not in dropped),
        accepted_patch_ids=accepted,
    )


def build_patch_id(*, origin: PatchOrigin, body: str, target_tag_keys: Sequence[str]) -> str:
    """Content-address a patch so recompiling identical advice reuses its identity."""

    return canonical_checksum(
        {
            "origin": origin,
            "body": body.strip(),
            "target_tag_keys": sorted(target_tag_keys),
        }
    )[:32]


def build_demo_id(*, subject_type: str, subject_id: int, rendered_text: str) -> str:
    return canonical_checksum(
        {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "rendered_text": rendered_text.strip(),
        }
    )[:32]


def artifact_from_payload(payload: Mapping[str, Any]) -> CompiledPromptArtifact:
    """Rebuild an artifact persisted by :meth:`CompiledPromptArtifact.as_payload`."""

    return CompiledPromptArtifact(
        baseline_prompt=str(payload["baseline_prompt"]),
        header=str(payload["header"]),
        compiler=payload["compiler"],
        compiler_version=str(payload["compiler_version"]),
        metric_version=str(payload["metric_version"]),
        patches=tuple(
            PromptPatch(
                patch_id=str(item["patch_id"]),
                kind=item["kind"],
                origin=item["origin"],
                ordinal=int(item["ordinal"]),
                body=str(item["body"]),
                rationale=str(item["rationale"]),
                target_tag_keys=tuple(item.get("target_tag_keys") or ()),
                gradient_text=item.get("gradient_text"),
                source_badcase_ids=tuple(item.get("source_badcase_ids") or ()),
                source_gold_label_ids=tuple(item.get("source_gold_label_ids") or ()),
            )
            for item in payload.get("patches") or ()
        ),
        demos=tuple(
            PromptDemo(
                demo_id=str(item["demo_id"]),
                gold_label_id=int(item["gold_label_id"]),
                subject_type=str(item["subject_type"]),
                subject_id=int(item["subject_id"]),
                rendered_text=str(item["rendered_text"]),
                redaction_mode=item["redaction_mode"],
                source_checksum=str(item["source_checksum"]),
                reception_id=item.get("reception_id"),
                segment_ids=tuple(item.get("segment_ids") or ()),
                recording_ids=tuple(item.get("recording_ids") or ()),
            )
            for item in payload.get("demos") or ()
        ),
        accepted_patch_ids=frozenset(payload.get("accepted_patch_ids") or ()),
    )
