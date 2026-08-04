"""The synchronous bridge DSPy and TextGrad call into.

Three things must hold or the optimizer becomes either unsafe or unaccountable:

* every call goes through ``execute_llm`` -- no adapter is ever touched directly,
  because that is what puts the spend in the durable ledger;
* the cache scope isolates optimizer probes from production tagging, since the v2
  recipe hash does not include ``purpose``;
* waiting on the event loop from the loop's own thread fails loudly instead of
  deadlocking a worker that would then look merely slow.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.optimizers.lm_bridge import (
    GatewayLM,
    LMBudget,
    LMBudgetExceededError,
    LMUsage,
    LoopReentryError,
    LoopRunner,
    OptimizerLMConfig,
)


class RecordingAdapter:
    """Records every request the gateway forwards, and nothing else."""

    def __init__(self, *, model: str = "test-llm", text: str = "候选指令") -> None:
        self.model = model
        self.text = text
        self.calls: list[dict[str, Any]] = []
        self.cost_microunits = 0
        self.usage: dict[str, int] = {"prompt_tokens": 40, "completion_tokens": 10}

    def cache_key(self, model: str, messages: Sequence[Mapping[str, Any]]) -> str:
        return json.dumps({"model": model, "messages": list(messages)}, ensure_ascii=False)

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return LLMResponse(
            text=self.text,
            model=self.model,
            prompt_hash="h" * 64,
            usage=dict(self.usage),
            cost_microunits=self.cost_microunits,
        )


def _config(**overrides: Any) -> OptimizerLMConfig:
    defaults: dict[str, Any] = {
        "tenant_id": "chang_an",
        "purpose": "prompt_lab_instruction_proposal",
        "compilation_id": 987_654_321,
    }
    return OptimizerLMConfig(**(defaults | overrides))


# --------------------------------------------------------------------- config


def test_a_purpose_outside_the_optimizer_namespace_is_refused() -> None:
    """Cost attribution is ``purpose LIKE 'prompt_lab_%'``; anything else is invisible."""

    with pytest.raises(ValueError, match="prompt_lab_"):
        _config(purpose="tag_extraction")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", ""),
        ("compilation_id", 0),
        ("max_tokens", 0),
        ("timeout_seconds", 0.0),
    ],
)
def test_a_config_that_could_not_be_honoured_is_refused(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_the_correlation_key_fits_the_ledger_column() -> None:
    # logical_request_id is String(64); a longer value would be truncated or rejected
    # at insert time, and the compile's spend would stop being queryable.
    config = _config(compilation_id=2**47)

    assert config.logical_request_id == f"prompt-compile:{2**47}"
    assert len(config.logical_request_id) <= 64


# ----------------------------------------------------------------- loop runner


@pytest.mark.asyncio
async def test_a_worker_thread_can_await_the_main_loop() -> None:
    runner = LoopRunner.for_running_loop()

    async def work() -> str:
        await asyncio.sleep(0)
        return "done"

    assert await asyncio.to_thread(runner.run, work()) == "done"


@pytest.mark.asyncio
async def test_calling_from_the_loops_own_thread_fails_instead_of_deadlocking() -> None:
    """The alternative is a worker that hangs forever and reports nothing."""

    runner = LoopRunner.for_running_loop()

    async def work() -> str:
        return "unreachable"

    with pytest.raises(LoopReentryError, match="工作线程"):
        runner.run(work())


@pytest.mark.asyncio
async def test_a_call_that_outlives_its_timeout_is_cancelled() -> None:
    """A coroutine left running keeps a provider connection and keeps billing."""

    runner = LoopRunner.for_running_loop()
    started = asyncio.Event()

    async def slow() -> str:
        started.set()
        await asyncio.sleep(30)
        return "too late"

    with pytest.raises(TimeoutError):
        await asyncio.to_thread(runner.run, slow(), timeout=0.05)

    assert started.is_set()


# ------------------------------------------------------------------ gateway lm


async def _complete(lm: GatewayLM, prompt: str = "改写这条规则") -> LLMResponse:
    return await asyncio.to_thread(lm.complete, [{"role": "user", "content": prompt}])


@pytest.mark.asyncio
async def test_a_completion_travels_through_the_gateway_to_the_adapter() -> None:
    adapter = RecordingAdapter()
    lm = GatewayLM(adapter=adapter, config=_config(), runner=LoopRunner.for_running_loop())

    response = await _complete(lm)

    assert response.text == "候选指令"
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["messages"] == [{"role": "user", "content": "改写这条规则"}]


@pytest.mark.asyncio
async def test_the_request_carries_the_isolating_cache_scope() -> None:
    """Without it an optimizer probe could be served a cached production result."""

    seen: list[Any] = []

    class CapturingAdapter(RecordingAdapter):
        async def execute(self, request: Any) -> LLMResponse:
            seen.append(request)
            return LLMResponse(text="ok", model=self.model, prompt_hash="h" * 64)

    lm = GatewayLM(
        adapter=CapturingAdapter(),  # type: ignore[arg-type]
        config=_config(),
        runner=LoopRunner.for_running_loop(),
    )
    await _complete(lm)

    request = seen[0]
    assert request.permission_scope == {"access_class": "tag_prompt_optimizer"}
    assert request.purpose == "prompt_lab_instruction_proposal"
    assert request.usage_context.logical_request_id == "prompt-compile:987654321"
    # Ledger failures must not fail open for a process whose job is to spend budget.
    assert request.usage_context.require_durable_ledger is True


@pytest.mark.asyncio
async def test_usage_accumulates_across_calls() -> None:
    adapter = RecordingAdapter()
    adapter.cost_microunits = 120
    lm = GatewayLM(adapter=adapter, config=_config(), runner=LoopRunner.for_running_loop())

    await _complete(lm)
    await _complete(lm, "再改一条")

    assert lm.usage.calls == 2
    assert lm.usage.total_tokens == 100
    assert lm.usage.cost_microunits == 240


def test_a_cached_response_adds_calls_but_no_cost() -> None:
    """Free reuse must not be recorded as spend, nor spend as free."""

    usage = LMUsage()
    usage.record(
        LLMResponse(
            text="缓存命中",
            model="test-llm",
            prompt_hash="h" * 64,
            cached=True,
            provider_called=False,
            provider_attempts=0,
            usage={"prompt_tokens": 40, "completion_tokens": 10},
        )
    )

    assert usage.calls == 1
    assert usage.provider_calls == 0
    assert usage.cost_microunits == 0
    assert usage.total_tokens == 50


@pytest.mark.asyncio
async def test_the_call_budget_stops_the_compile_before_the_next_call() -> None:
    adapter = RecordingAdapter()
    lm = GatewayLM(
        adapter=adapter,
        config=_config(),
        runner=LoopRunner.for_running_loop(),
        budget=LMBudget(max_calls=1),
    )

    await _complete(lm)
    with pytest.raises(LMBudgetExceededError, match="调用上限"):
        await _complete(lm)

    assert len(adapter.calls) == 1, "the refused call must not reach the provider"


@pytest.mark.asyncio
async def test_the_token_budget_stops_the_compile_once_it_is_spent() -> None:
    adapter = RecordingAdapter()
    lm = GatewayLM(
        adapter=adapter,
        config=_config(),
        runner=LoopRunner.for_running_loop(),
        budget=LMBudget(max_tokens=50),
    )

    await _complete(lm)
    with pytest.raises(LMBudgetExceededError, match="token 上限"):
        await _complete(lm)


@pytest.mark.asyncio
async def test_the_cost_budget_stops_the_compile_once_it_is_spent() -> None:
    adapter = RecordingAdapter()
    adapter.cost_microunits = 900
    lm = GatewayLM(
        adapter=adapter,
        config=_config(),
        runner=LoopRunner.for_running_loop(),
        budget=LMBudget(max_cost_microunits=800),
    )

    await _complete(lm)
    with pytest.raises(LMBudgetExceededError, match="成本上限"):
        await _complete(lm)


@pytest.mark.asyncio
async def test_an_empty_message_list_is_refused_before_a_request_is_built() -> None:
    adapter = RecordingAdapter()
    lm = GatewayLM(adapter=adapter, config=_config(), runner=LoopRunner.for_running_loop())

    with pytest.raises(ValueError, match="messages"):
        await asyncio.to_thread(lm.complete, [])

    assert adapter.calls == []


@pytest.mark.asyncio
async def test_the_single_turn_helper_puts_the_system_message_first() -> None:
    adapter = RecordingAdapter()
    lm = GatewayLM(adapter=adapter, config=_config(), runner=LoopRunner.for_running_loop())

    text = await asyncio.to_thread(lm.complete_text, "改写", system="你是提示词工程师")

    assert text == "候选指令"
    assert [message["role"] for message in adapter.calls[0]["messages"]] == ["system", "user"]


@pytest.mark.asyncio
async def test_the_single_turn_helper_omits_an_absent_system_message() -> None:
    adapter = RecordingAdapter()
    lm = GatewayLM(adapter=adapter, config=_config(), runner=LoopRunner.for_running_loop())

    await asyncio.to_thread(lm.complete_text, "改写")

    assert [message["role"] for message in adapter.calls[0]["messages"]] == ["user"]


@pytest.mark.asyncio
async def test_per_call_overrides_beat_the_injected_defaults() -> None:
    adapter = RecordingAdapter()
    lm = GatewayLM(
        adapter=adapter,
        config=_config(max_tokens=1_024, temperature=0.0),
        runner=LoopRunner.for_running_loop(),
    )

    await asyncio.to_thread(
        lm.complete,
        [{"role": "user", "content": "多给几个候选"}],
        max_tokens=64,
        temperature=0.7,
    )

    assert adapter.calls[0]["max_tokens"] == 64
    assert adapter.calls[0]["temperature"] == 0.7


def test_usage_serialises_every_dimension_the_budget_checks() -> None:
    # The compile record stores this mapping; a missing key would make a run that
    # hit its cap indistinguishable from one that finished early.
    payload = LMUsage().as_mapping()

    assert set(payload) == {
        "calls",
        "provider_calls",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost_microunits",
    }
