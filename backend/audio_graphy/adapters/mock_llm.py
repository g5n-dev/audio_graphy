"""Mock LLM adapter — deterministic, hash-keyed responses with optional flakiness.

Strategy:
- prompt_hash = MD5(model, messages) — same as real LLM cache key
- Returns one of several canned responses per hash bucket
- Simulates ~0.5% error rate by default (configurable)
- Honors cache_key: if caller provides the same key twice, second call returns `cached=True`
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from collections.abc import Sequence

from audio_graphy.adapters.protocols import LLMResponse

logger = logging.getLogger(__name__)

# Canned response library — keyed by prompt_hash prefix to vary outputs deterministically.
_RESPONSE_TEMPLATES: tuple[str, ...] = (
    # Entity extraction style
    '("实体","CS75 Plus","车型")\n("实体","张敏","坐席")\n("实体","5万元","价格方案")\n'
    '("关系","坐席","推荐","车型")\n("关系","客户","询问","车型")',
    # Tagging style
    '{"tag_path":"接待.开场","value":"标准","confidence":0.92}',
    # Q&A style
    "根据 7 月 1-15 日的 6 场接待录音分析，CS75 Plus 在 5 场中被提及，"
    "其中 4 场由坐席主动推荐，1 场由客户主动询问。",
    # Query rewrite style
    "关键词：CS75 Plus, 金融政策, 优惠, 试驾, 36期分期",
    # Summary style
    "本段对话中，坐席张敏向客户推荐了 CS75 Plus，"
    "介绍了全款优惠 5 万 + 36 期分期 + 2 年免息的价格方案。",
    # Generic fallback
    "（mock 响应）已根据 prompt 完成处理。",
)


class MockLLMAdapter:
    """Deterministic mock LLM with hash-keyed responses and optional error injection."""

    def __init__(
        self,
        *,
        model: str,
        error_rate: float = 0.005,
        latency_ms: int = 100,
    ) -> None:
        self.model = model
        self._error_rate = error_rate
        self._latency_ms = latency_ms
        self._call_count = 0
        # Local cache: cache_key → last response (simulates LLM response cache)
        self._cache: dict[str, LLMResponse] = {}

    @staticmethod
    def compute_prompt_hash(model: str, messages: Sequence[dict[str, str]]) -> str:
        """MD5 of (model, messages) — same formula VideoRAG uses for cache key."""
        payload = json.dumps({"model": model, "messages": list(messages)}, ensure_ascii=False)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
    ) -> LLMResponse:
        self._call_count += 1

        # Simulate error rate
        if random.random() < self._error_rate:
            raise RuntimeError(
                f"Mock LLM simulated error (rate={self._error_rate}, model={self.model})"
            )

        # Cache lookup
        prompt_hash = self.compute_prompt_hash(self.model, messages)
        if cache_key and cache_key in self._cache:
            cached = self._cache[cache_key]
            logger.debug("Mock LLM cache HIT for key=%s (model=%s)", cache_key[:8], self.model)
            return LLMResponse(
                text=cached.text,
                model=cached.model,
                prompt_hash=cached.prompt_hash,
                cached=True,
                usage=cached.usage,
            )

        # Simulate latency
        await asyncio.sleep(self._latency_ms / 1000.0)

        # Deterministic response selection — pick by prompt_hash prefix
        bucket = int(prompt_hash[:4], 16) % len(_RESPONSE_TEMPLATES)
        text = _RESPONSE_TEMPLATES[bucket]

        # Approximate token usage
        prompt_tokens = sum(len(m.get("content", "")) for m in messages) // 2
        completion_tokens = len(text) // 2

        response = LLMResponse(
            text=text,
            model=self.model,
            prompt_hash=prompt_hash,
            cached=False,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )

        # Store in cache if cache_key provided
        if cache_key:
            self._cache[cache_key] = response

        logger.debug(
            "Mock LLM call #%d (model=%s, hash=%s, bucket=%d, %d tokens)",
            self._call_count,
            self.model,
            prompt_hash[:8],
            bucket,
            completion_tokens,
        )

        return response

    @property
    def call_count(self) -> int:
        """Number of non-cached calls made — useful for test assertions."""
        return self._call_count
