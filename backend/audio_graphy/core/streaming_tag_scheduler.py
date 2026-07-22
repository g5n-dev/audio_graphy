"""StreamingTagScheduler — batch tag recompute trigger for streaming sessions.

M8 Phase 4 (WS-3 / T9). Per architecture §10: every N=5 confirmed segments
(L7 locked) trigger one batch LLM tag recompute via the M3
``RecomputeService.recompute_tags_for_segments()`` entry point. A debounce
window (default 500 ms, ``streaming_tag_debounce_ms``) prevents burst
triggers when several segments confirm within a short window.

Design notes:
    - One scheduler instance per WebSocket session (per-tenant isolation
      is implicit: each instance is bound to one tenant_id).
    - Does NOT modify M3 source paths — the new recompute entry point is
      appended to ``tags/recompute.py`` without touching the existing
      prompt-version-switch flow.
    - Thread-safety: one asyncio task per session, no locking required.

Typical use (ws_stream.py):
    scheduler = StreamingTagScheduler(recompute_svc, tenant_id=..., recording_id=...)
    result = await scheduler.on_segment_confirmed(segment_id)
    if result is not None:
        ...  # emit tags_updated event
    # on finalize:
    result = await scheduler.flush()
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from audio_graphy.tags.recompute import RecomputeService

logger = logging.getLogger(__name__)

DEFAULT_TAG_INTERVAL_N = 5  # L7 locked
DEFAULT_TAG_DEBOUNCE_MS = 500.0


@dataclass(frozen=True, slots=True)
class TagBatchResult:
    """Result of one streaming tag batch recompute.

    Attributes:
        tenant_id: Tenant scope.
        recording_id: Recording this batch belongs to.
        segment_ids: Segment ids included in this batch.
        tags_written: Number of tag facts written (0 on failure).
        error: Error message when the recompute failed (None on success).
    """

    tenant_id: str
    recording_id: int
    segment_ids: list[int] = field(default_factory=list)
    tags_written: int = 0
    error: str | None = None


class StreamingTagScheduler:
    """Trigger batch LLM tag recompute every N confirmed segments.

    Args:
        recompute_svc: M3 ``RecomputeService`` (or any object exposing an
            async ``recompute_tags_for_segments(tenant_id, recording_id,
            segment_ids)`` method — duck-typed for testability).
        interval_n: Trigger threshold in confirmed segments (L7 default 5).
        debounce_ms: Minimum spacing between triggers in milliseconds
            (default 500 — architecture §10.1).
        tenant_id: Tenant scope.
        recording_id: Recording being streamed.
    """

    def __init__(
        self,
        recompute_svc: RecomputeService,
        *,
        interval_n: int = DEFAULT_TAG_INTERVAL_N,
        debounce_ms: float = DEFAULT_TAG_DEBOUNCE_MS,
        tenant_id: str = "default",
        recording_id: int = 0,
    ) -> None:
        if interval_n < 1:
            raise ValueError(f"interval_n must be >= 1, got {interval_n}")
        if debounce_ms < 0:
            raise ValueError(f"debounce_ms must be >= 0, got {debounce_ms}")
        self._svc = recompute_svc
        self._interval = interval_n
        self._debounce_ms = debounce_ms
        self._tenant = tenant_id
        self._recording = recording_id
        self._since_last_trigger: int = 0
        self._last_trigger_at: float = 0.0
        self._pending_segment_ids: list[int] = []

    @property
    def tenant_id(self) -> str:
        return self._tenant

    @property
    def recording_id(self) -> int:
        return self._recording

    @property
    def pending_count(self) -> int:
        """Segments accumulated but not yet recomputed."""
        return len(self._pending_segment_ids)

    @property
    def trigger_count_since_last(self) -> int:
        """Confirmed segments seen since the last successful trigger."""
        return self._since_last_trigger

    async def on_segment_confirmed(self, segment_id: int) -> TagBatchResult | None:
        """Record one confirmed segment; trigger a batch when the threshold
        is reached and the debounce window has elapsed.

        Args:
            segment_id: DB id of the confirmed segment.

        Returns:
            TagBatchResult when a batch was triggered, else None.
        """
        self._since_last_trigger += 1
        self._pending_segment_ids.append(segment_id)
        if self._since_last_trigger < self._interval:
            return None
        # Debounce — if we triggered very recently, keep accumulating.
        now = time.monotonic()
        if self._last_trigger_at > 0.0 and (now - self._last_trigger_at) * 1000.0 < self._debounce_ms:
            return None
        return await self._trigger()

    async def flush(self) -> TagBatchResult | None:
        """Force-trigger a batch over remaining pending segments (session close).

        Returns:
            TagBatchResult when pending segments existed, else None.
        """
        if not self._pending_segment_ids:
            return None
        return await self._trigger()

    async def _trigger(self) -> TagBatchResult:
        """Invoke the M3 recompute over pending segments.

        Resets the accumulator regardless of outcome so a failed batch does
        not grow unboundedly; the error is surfaced in the result.
        """
        seg_ids = list(self._pending_segment_ids)
        try:
            result = await self._svc.recompute_tags_for_segments(
                tenant_id=self._tenant,
                recording_id=self._recording,
                segment_ids=seg_ids,
            )
            tags_written = int(getattr(result, "tags_written", 0) or 0)
            return TagBatchResult(
                tenant_id=self._tenant,
                recording_id=self._recording,
                segment_ids=seg_ids,
                tags_written=tags_written,
                error=None,
            )
        except Exception as exc:  # recompute failure must not kill the WS session
            logger.warning(
                "StreamingTagScheduler recompute failed tenant=%s recording=%s segments=%d: %s",
                self._tenant, self._recording, len(seg_ids), exc,
            )
            return TagBatchResult(
                tenant_id=self._tenant,
                recording_id=self._recording,
                segment_ids=seg_ids,
                tags_written=0,
                error=str(exc)[:200],
            )
        finally:
            self._pending_segment_ids = []
            self._since_last_trigger = 0
            self._last_trigger_at = time.monotonic()
