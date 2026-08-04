"""Adapters that let DSPy speak to this project's gateway. Forwarding only.

This file is excluded from coverage (see ``[tool.coverage.run] omit``) because CI
does not install the ``optimizer`` extra and it cannot even be imported without it.
That exclusion is only defensible while the file stays free of decisions: every
judgement -- budget, cache scope, ledger attribution, what a signature should say --
belongs in :mod:`audio_graphy.optimizers.lm_bridge` and
:mod:`audio_graphy.optimizers.signatures`, both of which are always testable.

If you find yourself adding an ``if`` here, it belongs on the other side of the line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import dspy
from dspy.signatures.signature import make_signature

from audio_graphy.optimizers.lm_bridge import GatewayLM
from audio_graphy.optimizers.signatures import SignatureSpec

if TYPE_CHECKING:  # pragma: no cover
    from audio_graphy.adapters.protocols import LLMResponse

_ANNOTATIONS: dict[str, Any] = {
    "str": str,
    "list[str]": list[str],
    "list[dict]": list[dict[str, Any]],
    "dict": dict[str, Any],
    "int": int,
    "float": float,
}


class _Message:
    __slots__ = ("content",)

    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    __slots__ = ("message",)

    def __init__(self, content: str) -> None:
        self.message = _Message(content)


class _CompletionLike:
    """The OpenAI-chat-completion shape ``BaseLM._process_completion`` reads.

    ``model`` is not optional: the history entry accesses it directly rather than
    through ``getattr``.
    """

    __slots__ = ("choices", "model", "usage")

    def __init__(self, *, text: str, model: str, usage: dict[str, int]) -> None:
        self.choices = [_Choice(text)]
        self.model = model
        self.usage = usage


# dspy carries no type information here -- CI never installs it, so mypy resolves the
# package to Any and refuses the subclass. The ignore is scoped to that one fact.
class GatewayDSPyLM(dspy.BaseLM):  # type: ignore[misc]
    """A ``dspy.BaseLM`` that routes every completion through the gateway.

    ``cache=False`` is deliberate. DSPy's own disk cache would serve repeats without
    telling the gateway, and the durable ledger would then under-report a compile's
    spend. The gateway has its own cache, and that one is accounted for.
    """

    def __init__(self, lm: GatewayLM) -> None:
        super().__init__(
            model=lm.adapter.model,
            model_type="chat",
            temperature=lm.config.temperature,
            max_tokens=lm.config.max_tokens,
            cache=False,
        )
        self._lm = lm

    @property
    def gateway_lm(self) -> GatewayLM:
        return self._lm

    def forward(
        self,
        prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> _CompletionLike:
        resolved: list[dict[str, Any]]
        if messages:
            resolved = [dict(message) for message in messages]
        elif prompt is not None:
            resolved = [{"role": "user", "content": prompt}]
        else:
            raise ValueError("DSPy called the LM with neither a prompt nor messages")

        response: LLMResponse = self._lm.complete(
            resolved,
            max_tokens=kwargs.get("max_tokens"),
            temperature=kwargs.get("temperature"),
        )
        return _CompletionLike(
            text=response.text,
            model=response.model,
            usage=dict(response.usage or {}),
        )


def to_dspy_signature(spec: SignatureSpec) -> type[dspy.Signature]:
    """Turn a described signature into the DSPy class that implements it."""

    fields: dict[str, tuple[Any, Any]] = {}
    for field in spec.inputs:
        fields[field.name] = (
            _ANNOTATIONS.get(field.annotation, str),
            dspy.InputField(desc=field.description),
        )
    for field in spec.outputs:
        fields[field.name] = (
            _ANNOTATIONS.get(field.annotation, str),
            dspy.OutputField(desc=field.description),
        )
    return cast("type[dspy.Signature]", make_signature(fields, spec.instructions, spec.name))
