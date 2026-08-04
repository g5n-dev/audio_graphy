"""Describe the tagging task in the shape a DSPy signature needs.

This module computes *what the signature should say* and stops there. Turning the
description into a ``dspy.Signature`` object is two lines in ``dspy_bridge``, which
is excluded from coverage because CI does not install the extra -- so anything with
a decision in it has to live on this side of the line.

The descriptions are built from the tenant's own tag definitions, never from a
hard-coded list. A tenant can add a tag between two compiles, and a signature that
did not follow would ask the model for fields the schema no longer has.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: DSPy renders field descriptions into the prompt verbatim, so they are subject to
#: the same input budget as everything else. Definitions with long prose get cut
#: rather than silently pushing the prompt over its ceiling.
_MAX_DESCRIPTION_CHARS = 240

_ASSIGNMENT_DESCRIPTION = (
    "判定结果数组。每个元素含 tag_key、value、confidence 与 evidence_segment_ids；"
    "没有直接文本依据的标签一律省略，不要猜测。"
)


class SignatureSpecError(ValueError):
    """Raised when a signature cannot be described from the definitions given."""


@dataclass(frozen=True, slots=True)
class SignatureField:
    name: str
    description: str
    annotation: str


@dataclass(frozen=True, slots=True)
class SignatureSpec:
    """A DSPy signature, described without depending on DSPy."""

    name: str
    instructions: str
    inputs: tuple[SignatureField, ...]
    outputs: tuple[SignatureField, ...]

    def as_mapping(self) -> dict[str, Any]:
        """Serialisable form, recorded on the artifact so a compile can be replayed."""

        return {
            "name": self.name,
            "instructions": self.instructions,
            "inputs": [
                {"name": f.name, "description": f.description, "annotation": f.annotation}
                for f in self.inputs
            ],
            "outputs": [
                {"name": f.name, "description": f.description, "annotation": f.annotation}
                for f in self.outputs
            ],
        }


def _truncate(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_DESCRIPTION_CHARS:
        return collapsed
    return collapsed[: _MAX_DESCRIPTION_CHARS - 1] + "…"


def describe_tag(tag_key: str, definition: Mapping[str, Any] | None) -> str:
    """One line telling the model what a tag means and what it may be set to."""

    if not isinstance(definition, Mapping):
        return f"标签 {tag_key}。"
    parts: list[str] = []
    description = definition.get("description")
    if isinstance(description, str) and description.strip():
        parts.append(description.strip())
    allowed = definition.get("allowed_values")
    # A str is a Sequence; treating one as a value list would spell it letter by
    # letter into the prompt.
    if isinstance(allowed, Sequence) and not isinstance(allowed, str | bytes):
        values = [str(value) for value in allowed]
        if values:
            parts.append(f"合法取值：{'、'.join(values)}。")
    value_type = definition.get("value_type")
    if isinstance(value_type, str) and value_type and not parts:
        parts.append(f"取值类型：{value_type}。")
    return _truncate(f"标签 {tag_key}。" + "".join(parts))


def build_tagging_signature(
    *,
    instructions: str,
    definitions: Mapping[str, Mapping[str, Any]],
    target_tag_keys: Sequence[str] = (),
) -> SignatureSpec:
    """Describe the signature a tagging predictor should run under.

    *target_tag_keys* narrows the signature to the tags a compile is actually about.
    An empty sequence means every defined tag, sorted -- the order has to be stable
    or two identical compiles would produce different prompts and different
    checksums.
    """

    if not instructions.strip():
        raise SignatureSpecError("signature instructions must not be empty")

    keys = list(target_tag_keys) if target_tag_keys else sorted(definitions)
    unknown = [key for key in keys if key not in definitions]
    if unknown:
        # Asking for a tag with no definition would leave the model to invent both
        # the meaning and the value space.
        raise SignatureSpecError(f"no definition for tag keys: {'、'.join(sorted(unknown))}")
    if not keys:
        raise SignatureSpecError("a tagging signature needs at least one tag definition")

    catalogue = "\n".join(f"- {key}：{describe_tag(key, definitions[key])}" for key in keys)
    return SignatureSpec(
        name="TagDialogueUnit",
        instructions=_truncate(instructions),
        inputs=(
            SignatureField(
                name="dialogue",
                description="待判定的对话文本，按 segment 顺序给出。",
                annotation="str",
            ),
            SignatureField(
                name="tag_catalogue",
                description=f"可用标签及其判定标准：\n{catalogue}",
                annotation="str",
            ),
        ),
        outputs=(
            SignatureField(
                name="assignments",
                description=_ASSIGNMENT_DESCRIPTION,
                annotation="list[dict]",
            ),
        ),
    )
