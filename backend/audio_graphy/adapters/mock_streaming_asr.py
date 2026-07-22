"""Mock streaming ASR adapter — deterministic ASRDeltaResult from sha512(pcm).

M8 Phase 4 (WS-1 / T3). CI-friendly counterpart to
``adapters/real/streaming_funasr.py``. Produces the same ASRDeltaResult
shape without the WebSocket connection.

Determinism contract (architecture §5.2):

    - Same PCM bytes → same delta text + sentence_id.
    - Every ``realtime_interval`` push_pcm() (default 4): emit one realtime delta.
    - Every ``confirmed_interval`` push_pcm() (default 12): emit one confirmed delta.
    - Corpus of confirmed texts cycles through a fixed list so the
      VAD → ASR → confirmed segment pipeline is fully testable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence

from audio_graphy.adapters.protocols import (
    ASRDeltaResult,
    StreamingASRAdapter,
)

logger = logging.getLogger(__name__)

# Mock corpus for confirmed deltas — short Chinese sentences typical of
# store customer-service recordings (car-sales / telecom / catering).
_CONFIRMED_CORPUS: tuple[str, ...] = (
    "我想退订现在的套餐。",
    "请问有什么优惠活动？",
    "这个车型的价格是多少？",
    "我对你们的服务不满意。",
    "好的，我考虑一下再回复。",
    "麻烦帮我转接到人工客服。",
)

# Mock corpus for realtime partials (prefixes of confirmed texts).
_REALTIME_PARTIALS: tuple[str, ...] = (
    "我想",
    "我想退",
    "我想退订",
    "请问",
    "请问有",
    "请问有什么",
)


class MockStreamingASRAdapter:
    """Deterministic mock streaming ASR — no WebSocket, no GPU, CI-friendly.

    Pattern:
        - Every ``realtime_interval`` push_pcm() yields a realtime delta.
        - Every ``confirmed_interval`` push_pcm() yields a confirmed delta.

    Args:
        connect_latency_ms: Simulated handshake latency.
        push_latency_ms: Simulated per-push latency.
        realtime_interval: Push index that triggers a realtime delta (default 4).
        confirmed_interval: Push index that triggers a confirmed delta (default 12).
        flaky: When True, every 100th push raises a synthetic error.
    """

    def __init__(
        self,
        *,
        connect_latency_ms: float = 20.0,
        push_latency_ms: float = 50.0,
        realtime_interval: int = 4,
        confirmed_interval: int = 12,
        flaky: bool = False,
    ) -> None:
        self._connect_latency_sec = connect_latency_ms / 1000.0
        self._push_latency_sec = push_latency_ms / 1000.0
        self._realtime_interval = max(1, realtime_interval)
        self._confirmed_interval = max(self._realtime_interval + 1, confirmed_interval)
        self._flaky = flaky

        self._push_count = 0
        self._realtime_count = 0
        self._confirmed_count = 0
        self._sentence_id = 0
        self._connected = False
        self._closed = False

    # --------------------------------------------------------------
    # Protocol methods
    # --------------------------------------------------------------
    async def connect(
        self,
        *,
        session_id: str,
        tenant_id: str,
        hotwords: Sequence[str] = (),
    ) -> None:
        """Simulate the handshake."""
        await asyncio.sleep(self._connect_latency_sec)
        self._connected = True
        self._closed = False
        logger.debug(
            "MockStreamingASR connect session=%s tenant=%s hotwords=%d",
            session_id, tenant_id, len(hotwords),
        )

    async def push_pcm(self, pcm: bytes, *, seq: int) -> ASRDeltaResult:
        """Send one PCM chunk and yield the next deterministic delta."""
        if not self._connected or self._closed:
            from audio_graphy.adapters.exceptions import StreamingASRServerError

            raise StreamingASRServerError(
                "push_pcm called before connect() or after aclose()",
            )

        await asyncio.sleep(self._push_latency_sec)
        self._push_count += 1

        if self._flaky and self._push_count % 100 == 0:
            from audio_graphy.adapters.exceptions import StreamingASRServerError

            raise StreamingASRServerError(
                f"MockStreamingASR flaky mode triggered at push={self._push_count}",
            )

        # Deterministic seed from pcm → pick a corpus index.
        digest = hashlib.sha512(pcm).hexdigest()
        corpus_idx = int(digest[:4], 16) % max(1, len(_CONFIRMED_CORPUS))

        # Decide whether this push produces a confirmed delta.
        if self._push_count % self._confirmed_interval == 0:
            self._confirmed_count += 1
            self._sentence_id += 1
            text = _CONFIRMED_CORPUS[corpus_idx]
            return ASRDeltaResult(
                seq=seq,
                mode="confirmed",
                text=text,
                is_final=True,
                sentence_id=self._sentence_id,
                confidence=0.95,
            )

        # Realtime delta?
        if self._push_count % self._realtime_interval == 0:
            self._realtime_count += 1
            partial = _REALTIME_PARTIALS[corpus_idx % len(_REALTIME_PARTIALS)]
            return ASRDeltaResult(
                seq=seq,
                mode="realtime",
                text=partial,
                is_final=False,
                sentence_id=self._sentence_id,
                confidence=0.80,
            )

        # No delta this push — return an empty realtime delta so the caller
        # can continue without special-casing the gap.
        return ASRDeltaResult(
            seq=seq,
            mode="realtime",
            text="",
            is_final=False,
            sentence_id=self._sentence_id,
            confidence=0.0,
        )

    async def finalize(self) -> tuple[ASRDeltaResult, ...]:
        """Simulate drain — emit one trailing confirmed delta if applicable."""
        if not self._connected or self._closed:
            return ()
        # Synthesise one final confirmed delta to flush the current sentence.
        self._sentence_id += 1
        self._confirmed_count += 1
        # Empty push_count guard against re-entry after finalize.
        text = _CONFIRMED_CORPUS[self._confirmed_count % len(_CONFIRMED_CORPUS)]
        return (
            ASRDeltaResult(
                seq=-1,  # synthetic seq at finalize
                mode="confirmed",
                text=text,
                is_final=True,
                sentence_id=self._sentence_id,
                confidence=0.95,
            ),
        )

    async def aclose(self) -> None:
        """No-op beyond flag-setting (no real resources)."""
        self._closed = True
        self._connected = False

    # ------------------------------------------------------------------
    # Test-only accessors (NOT part of the StreamingASRAdapter Protocol).
    # ------------------------------------------------------------------
    @property
    def push_count(self) -> int:
        return self._push_count

    @property
    def realtime_count(self) -> int:
        return self._realtime_count

    @property
    def confirmed_count(self) -> int:
        return self._confirmed_count


# Protocol satisfaction check (fails at import if drift).
_MOCK_STREAMING_ASR_PROTOCOL_CHECK: StreamingASRAdapter = MockStreamingASRAdapter()
