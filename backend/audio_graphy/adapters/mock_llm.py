"""Mock LLM adapter — deterministic, hash-keyed responses with optional flakiness.

Strategy:
- prompt_hash = SHA-256(model, messages) — same as the real transport
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
from collections.abc import Mapping, Sequence
from typing import Any

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
        model_epoch: str | None = None,
        error_rate: float = 0.005,
        latency_ms: int = 100,
    ) -> None:
        self.model = model
        self.model_epoch = model_epoch or model
        self.provider = "mock"
        self._error_rate = error_rate
        self._latency_ms = latency_ms
        self._call_count = 0
        # Local cache: cache_key → last response (simulates LLM response cache)
        self._cache: dict[str, LLMResponse] = {}

    @staticmethod
    def compute_prompt_hash(model: str, messages: Sequence[dict[str, str]]) -> str:
        """SHA-256 of ``(model, messages)`` for deterministic correlation."""
        payload = json.dumps({"model": model, "messages": list(messages)}, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        top_p: float = 1.0,
        seed: int | None = None,
        stop: Sequence[str] = (),
        tools: Sequence[Mapping[str, Any]] = (),
        response_format: Mapping[str, Any] | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        del temperature, max_tokens, top_p, seed, stop, tools, response_format, response_schema
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
                cache_source="mock_adapter",
                provider_called=False,
            )

        # Simulate latency
        await asyncio.sleep(self._latency_ms / 1000.0)

        structured_tags = self._legacy_tag_batch_response(messages)
        bucket = int(prompt_hash[:4], 16) % len(_RESPONSE_TEMPLATES)
        text = structured_tags if structured_tags is not None else _RESPONSE_TEMPLATES[bucket]

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

    def _legacy_tag_batch_response(
        self,
        messages: Sequence[dict[str, str]],
    ) -> str | None:
        """Return the structured response expected by ``LegacyTagBatcher``."""

        is_batch_prompt = any(
            message.get("role") == "system" and "门店接待质检分类器" in message.get("content", "")
            for message in messages
        )
        if not is_batch_prompt:
            return None
        user_content = next(
            (message.get("content", "") for message in messages if message.get("role") == "user"),
            "",
        )
        try:
            payload = json.loads(user_content)
        except json.JSONDecodeError:
            return None
        tag_paths = (
            payload.get("k", payload.get("tag_paths"))
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(tag_paths, list) or not all(
            isinstance(path, str) and path for path in tag_paths
        ):
            return None
        transcript = str(payload.get("t", payload.get("transcript", "")))
        rows = []
        for path in tag_paths:
            digest = hashlib.sha256(f"{self.model}\0{path}\0{transcript}".encode()).digest()
            rows.append(
                {
                    "tag_path": path,
                    "value": "pass" if digest[0] % 2 == 0 else "fail",
                    "confidence": 0.95,
                }
            )
        return json.dumps({"tags": rows}, ensure_ascii=False, separators=(",", ":"))

    @property
    def call_count(self) -> int:
        """Number of non-cached calls made — useful for test assertions."""
        return self._call_count
