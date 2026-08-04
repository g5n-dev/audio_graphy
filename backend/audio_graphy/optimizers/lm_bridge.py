"""Run gateway-backed model calls from the synchronous code DSPy and TextGrad expect.

Both libraries are synchronous top to bottom, while every model call in this project
is an ``await execute_llm(...)`` bound to the worker's event loop. The bridge is a
thread hop, never a new event loop::

    optimizer_worker                       [main loop]
      └─ await asyncio.to_thread(proposer.compile_sync, ...)
            └─ GatewayLM.complete()        [sync, worker thread]
                  └─ LoopRunner.run(...)   [back on the main loop]

``asyncio.run()`` inside the worker thread would build a *second* loop, and both the
SQLAlchemy async engine and the httpx client the gateway holds are bound to the
first one -- the failure is not a clean error but cross-loop corruption.

Nothing here imports dspy or textgrad. The bridge stays testable on a machine
without the optional extras, and the vendor adapters that do import them (see
``dspy_bridge``) are left with nothing to do but forward.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, TypeVar

from audio_graphy.adapters.protocols import LLMAdapter, LLMResponse
from audio_graphy.llm.gateway import LLMRequest, LLMUsageContext, execute_llm

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Every purpose this bridge emits starts here. Cost attribution for prompt-lab work
#: is ``purpose LIKE 'prompt_lab_%'`` against the durable ledger, so a purpose that
#: skips the prefix is spend that silently leaves the optimizer's books.
PURPOSE_PREFIX = "prompt_lab_"

#: Cache isolation. The v2 recipe hash covers ``messages`` and ``permission_scope``
#: but not ``purpose``, so the scope is what keeps an optimizer probe from being
#: served a cached production tagging result -- and vice versa.
_ACCESS_CLASS = "tag_prompt_optimizer"

_LOGICAL_REQUEST_PREFIX = "prompt-compile:"


class LoopReentryError(RuntimeError):
    """Raised when a bridge call would block the very loop it is waiting on."""


class LMBudgetExceededError(RuntimeError):
    """Raised when a compile has spent the budget it was granted."""


@dataclass(frozen=True, slots=True)
class OptimizerLMConfig:
    """Everything the bridge needs that would otherwise come from global config.

    ``optimizers`` sits below the layer that may read settings, so the worker builds
    this and injects it. That is not only a layering rule: a compile must record the
    tier and caps it actually ran with, and a value pulled from process config at
    call time cannot be reconstructed afterwards.
    """

    tenant_id: str
    purpose: str
    compilation_id: int
    model_tier: str = "strong"
    temperature: float = 0.0
    max_tokens: int = 1_024
    timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must not be empty")
        if not self.purpose.startswith(PURPOSE_PREFIX):
            raise ValueError(f"optimizer purposes must start with {PURPOSE_PREFIX!r}")
        if self.compilation_id < 1:
            raise ValueError("compilation_id must be a positive integer")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def logical_request_id(self) -> str:
        """Correlation key for the ledger.

        ``LLMUsageContext`` has no compilation field, and borrowing
        ``optimization_run_id`` would write an id that points at no optimization run
        -- any later join would match nothing, or worse, match by coincidence.
        ``(tenant_id, logical_request_id)`` is indexed, so this stays queryable.
        """

        return f"{_LOGICAL_REQUEST_PREFIX}{self.compilation_id}"


@dataclass(slots=True)
class LMUsage:
    """What a compile has spent so far, as reported by the gateway."""

    calls: int = 0
    provider_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_microunits: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def record(self, response: LLMResponse) -> None:
        self.calls += 1
        self.provider_calls += int(response.provider_attempts)
        usage = response.usage or {}
        self.prompt_tokens += int(usage.get("prompt_tokens", 0))
        self.completion_tokens += int(usage.get("completion_tokens", 0))
        # A cached response reports zero cost, so this stays honest for free reuse.
        self.cost_microunits += int(response.cost_microunits)

    def as_mapping(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "provider_calls": self.provider_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_microunits": self.cost_microunits,
        }


@dataclass(frozen=True, slots=True)
class LMBudget:
    """Caps for one compile. ``None`` means the dimension is not capped here.

    Enforcement is *before* each call, against what is already spent. Token and cost
    figures only exist once a response comes back, so the last call may cross a cap;
    the overshoot is bounded by one call and the alternative -- refusing to record a
    call that already happened -- would understate the ledger.
    """

    max_calls: int | None = None
    max_tokens: int | None = None
    max_cost_microunits: int | None = None

    def check(self, usage: LMUsage) -> None:
        if self.max_calls is not None and usage.calls >= self.max_calls:
            raise LMBudgetExceededError(f"编译已用满 {self.max_calls} 次模型调用上限")
        if self.max_tokens is not None and usage.total_tokens >= self.max_tokens:
            raise LMBudgetExceededError(
                f"编译已用满 {self.max_tokens} token 上限（已用 {usage.total_tokens}）"
            )
        if (
            self.max_cost_microunits is not None
            and usage.cost_microunits >= self.max_cost_microunits
        ):
            raise LMBudgetExceededError(
                f"编译已用满 {self.max_cost_microunits} 微单位成本上限"
                f"（已用 {usage.cost_microunits}）"
            )


class LoopRunner:
    """Submit coroutines to a loop running on another thread and wait for them."""

    __slots__ = ("_loop",)

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @classmethod
    def for_running_loop(cls) -> LoopRunner:
        """Capture the loop of the caller. Call this *before* handing off to a thread."""

        return cls(asyncio.get_running_loop())

    def run(self, coro: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            # Waiting on the future would block the loop that has to complete it.
            # Closing the coroutine first keeps "never awaited" out of the traceback,
            # which under filterwarnings=error would replace this message with a
            # far less informative one.
            coro.close()
            raise LoopReentryError(
                "LoopRunner.run 必须在工作线程里调用；在事件循环自己的线程上调用会永久阻塞该循环。"
            )

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except FutureTimeoutError:
            # The coroutine keeps running otherwise, still holding a provider
            # connection and still billing against this compile's budget.
            future.cancel()
            raise


@dataclass
class GatewayLM:
    """A synchronous chat-completion callable that goes through the LLM gateway.

    Deliberately not a DSPy or TextGrad type. Those adapters wrap this one, so the
    accounting, the cache scope and the budget live in code that can be tested
    without either library installed.
    """

    adapter: LLMAdapter
    config: OptimizerLMConfig
    runner: LoopRunner
    budget: LMBudget = field(default_factory=LMBudget)
    usage: LMUsage = field(default_factory=LMUsage)

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop: Sequence[str] = (),
    ) -> LLMResponse:
        """Run one completion, blocking the calling (worker) thread until it lands."""

        if not messages:
            raise ValueError("messages must not be empty")
        self.budget.check(self.usage)

        request = LLMRequest(
            tenant_id=self.config.tenant_id,
            purpose=self.config.purpose,
            messages=tuple(dict(message) for message in messages),
            model_tier=self.config.model_tier,
            temperature=self.config.temperature if temperature is None else temperature,
            max_tokens=self.config.max_tokens if max_tokens is None else max_tokens,
            stop=tuple(stop),
            permission_scope={"access_class": _ACCESS_CLASS},
            usage_context=LLMUsageContext(
                logical_request_id=self.config.logical_request_id,
                # Accounting gaps are not acceptable for a process whose whole point
                # is to spend a granted budget: make ledger failures loud.
                require_durable_ledger=True,
            ),
        )
        response = self.runner.run(
            execute_llm(self.adapter, request),
            timeout=self.config.timeout_seconds,
        )
        self.usage.record(response)
        logger.debug(
            "optimizer lm call purpose=%s cached=%s tokens=%d cost=%d",
            self.config.purpose,
            response.cached,
            self.usage.total_tokens,
            self.usage.cost_microunits,
        )
        return response

    def complete_text(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
        """Convenience wrapper for the single-turn shape both libraries mostly use."""

        messages: list[Mapping[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.complete(messages, **kwargs).text
