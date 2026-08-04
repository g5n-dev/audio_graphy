"""Adapters that let TextGrad speak to this project's gateway. Forwarding only.

Excluded from coverage (see ``[tool.coverage.run] omit``) because CI does not install
the ``textgrad`` extra and this file cannot be imported without it. That exclusion
holds only while the file stays free of decisions: the prompts, the constraints, what
counts as a usable edit and when a result is too thin to trust all live in
:mod:`audio_graphy.optimizers.gradients`, which is always testable.

Three upstream behaviours are load-bearing and easy to get wrong:

* ``Variable.backward(engine)`` raises if a global backward engine is *also* set. The
  engine is always passed explicitly here and ``set_backward_engine`` is never called
  -- a global would also make two concurrent compiles share one engine.
* ``TextualGradientDescent.step()`` mutates the ``Variable`` in place and returns
  nothing, so the edited rule is read back off the variable afterwards.
* One iteration is three generations, and **only two of them run under prompts this
  project wrote**. The loss forward pass uses the evaluation instruction from
  ``gradients``, and the descent step uses ``TGD_SYSTEM`` via
  ``optimizer_system_prompt``; the backward pass in between runs under TextGrad's own
  English meta-prompt, which the library does not expose. So ``gradient_text`` is
  shaped by our evaluation instruction but phrased under theirs -- worth knowing
  before treating the wording as something this repo controls.
"""

from __future__ import annotations

from typing import Any

import textgrad as tg
from textgrad.engine import EngineLM

from audio_graphy.optimizers.gradients import (
    EVALUATION_SYSTEM,
    TGD_CONSTRAINTS,
    TGD_SYSTEM,
    GradientError,
    GradientOutcome,
)
from audio_graphy.optimizers.lm_bridge import GatewayLM


class GatewayTextGradEngine(EngineLM):  # type: ignore[misc]
    """A ``textgrad`` engine that routes every generation through the gateway.

    TextGrad has no cache of its own to disable, but it does default
    ``system_prompt`` on the class. It is set per call instead, so the critique and
    the descent step cannot silently inherit each other's instructions.
    """

    def __init__(self, lm: GatewayLM) -> None:
        self._lm = lm
        self.model_string = lm.adapter.model
        self.system_prompt = EVALUATION_SYSTEM

    @property
    def gateway_lm(self) -> GatewayLM:
        return self._lm

    def generate(self, prompt: Any, system_prompt: str | None = None, **kwargs: Any) -> str:
        if not isinstance(prompt, str):
            # Multimodal variables carry a list of parts; this project has no image
            # path into a prompt and must not silently stringify one.
            raise GradientError("TextGrad asked for a non-text generation")
        return self._lm.complete_text(
            prompt,
            system=system_prompt or self.system_prompt,
            max_tokens=kwargs.get("max_tokens"),
            temperature=kwargs.get("temperature"),
        )

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        return self.generate(*args, **kwargs)


class LibraryGradientStep:
    """Run the real TextGrad loop for one rule.

    One iteration is three provider calls: the loss forward pass, the backward pass
    that produces the gradient text, and the optimizer step that rewrites the rule.
    The caller's budget is what bounds ``iterations``.
    """

    def __init__(self, engine: GatewayTextGradEngine) -> None:
        self._engine = engine

    def run(
        self,
        *,
        current_rule: str,
        evaluation_prompt: str,
        role_description: str,
        iterations: int,
    ) -> GradientOutcome:
        if iterations < 1:
            raise GradientError("textgrad iterations must be at least 1")

        variable = tg.Variable(
            current_rule,
            requires_grad=True,
            role_description=role_description,
        )
        loss_fn = tg.TextLoss(evaluation_prompt, engine=self._engine)
        optimizer = tg.TGD(
            parameters=[variable],
            engine=self._engine,
            constraints=list(TGD_CONSTRAINTS),
            optimizer_system_prompt=_TGD_TEMPLATE,
        )

        diagnoses: list[str] = []
        for _ in range(iterations):
            optimizer.zero_grad()
            loss = loss_fn(variable)
            loss.backward(self._engine)
            diagnoses.extend(gradient.value for gradient in variable.gradients)
            optimizer.step()

        return GradientOutcome(
            gradient_text="\n".join(text for text in diagnoses if text),
            proposed_edit=variable.value,
            rounds=iterations,
        )


#: TextGrad substitutes the tag names into its optimizer prompt, so the placeholders
#: are part of the contract rather than decoration -- dropping them leaves the model
#: with no way to delimit its answer and ``step()`` silently keeps the old rule.
_TGD_TEMPLATE = (
    TGD_SYSTEM
    + "\n\n必须把改写后的规则放在 {new_variable_start_tag} 与 {new_variable_end_tag} 之间，"
    "标签之间的内容会直接替换原规则。"
)


__all__ = ["GatewayTextGradEngine", "LibraryGradientStep"]
