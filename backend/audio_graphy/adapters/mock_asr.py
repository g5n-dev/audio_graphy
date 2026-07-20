"""Mock ASR adapter — returns deterministic Chinese transcripts.

Strategy:
- Reads audio file hash to pick from a fixed library of store-conversation scripts
- ~1% flakiness rate when `flaky=True` (for testing retry logic)
- Returns character-level timing approximations
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random

from audio_graphy.adapters.protocols import ASRResult, VADSegment

logger = logging.getLogger(__name__)

# Library of canned Chinese store-conversation transcripts.
# Used to deterministically pick a transcript based on file hash.
_STORE_SCRIPTS: tuple[str, ...] = (
    "您好，欢迎光临，我是销售顾问张敏。请问您今天看什么车型？",
    "我想了解一下 CS75 Plus，听说最近有优惠活动。",
    "是的，CS75 Plus 现在全款优惠 5 万元，还可以选 36 期分期，搭配 2 年免息金融政策。",
    "另外我们赠送 3 次保养，试驾预约也方便，您看什么时候有空？",
    "那 UNI-V 呢？我朋友开的是 UNI-V，对比一下哪款更适合我。",
    "UNI-V 也有 36 期分期方案，金融政策略有不同，我们详细对比一下。",
    "哈弗 H6 和博越 L 也是热门竞品，要不要一起看看？",
    "好的，那我们先预约试驾 CS75 Plus，金融方案我整理一下发您。",
    "好的，谢谢您的接待，回头我跟家人商量一下再决定。",
    "不客气，期待您的好消息，需要任何信息随时联系我。",
)


class MockASRAdapter:
    """Deterministic mock ASR — picks a transcript from a fixed library by file hash."""

    def __init__(self, *, flaky: bool = False, latency_ms: int = 200) -> None:
        self._flaky = flaky
        self._latency_ms = latency_ms
        self._call_count = 0

    @property
    def call_count(self) -> int:
        """Number of transcribe calls made — useful for test assertions."""
        return self._call_count

    async def transcribe(
        self,
        audio_path: str,
        *,
        segments: list[VADSegment] | None = None,
        language: str = "zh",
    ) -> ASRResult:
        self._call_count += 1

        # Simulate ~1% flakiness
        if self._flaky and random.random() < 0.01:
            raise RuntimeError("Mock ASR simulated timeout (flaky=True)")

        await asyncio.sleep(self._latency_ms / 1000.0)

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        # Pick a deterministic transcript from the library
        file_hash = hashlib.md5(f"{audio_path}:{os.path.getsize(audio_path)}".encode()).hexdigest()
        seed = int(file_hash[:8], 16)
        rng = random.Random(seed)
        text = rng.choice(_STORE_SCRIPTS)

        # Approximate word-level timestamps (1 char per ~0.25s)
        char_count = len(text)
        words: list[tuple[str, float, float]] = []
        t = 0.0
        for ch in text:
            dur = 0.25 if ch not in "，。？！、" else 0.15
            words.append((ch, t, t + dur))
            t += dur

        logger.debug(
            "Mock ASR transcribed %s → %d chars (call #%d)",
            audio_path,
            char_count,
            self._call_count,
        )

        return ASRResult(
            text=text,
            language=language,
            confidence=round(rng.uniform(0.92, 0.99), 3),
            words=tuple(words),
        )
