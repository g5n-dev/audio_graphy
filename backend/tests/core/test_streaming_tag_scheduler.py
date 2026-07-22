"""M8 Phase 4 — StreamingTagScheduler unit tests (T9).

Covers:
    - N=5 interval trigger semantics (L7).
    - Debounce behaviour (burst suppression).
    - flush() on session close.
    - Tenant isolation (two schedulers do not share state).
    - Error resilience (recompute failure returns error result, state reset).
    - Constructor validation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from audio_graphy.core.streaming_tag_scheduler import (
    StreamingTagScheduler,
    TagBatchResult,
)

# ============================================================
# Fakes
# ============================================================


@dataclass
class _FakeBatchResult:
    tags_written: int = 3


class _FakeRecomputeService:
    """Records calls to recompute_tags_for_segments."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._fail = fail

    async def recompute_tags_for_segments(
        self,
        tenant_id: str,
        recording_id: int,
        segment_ids: list[int],
    ) -> _FakeBatchResult:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "recording_id": recording_id,
                "segment_ids": list(segment_ids),
            }
        )
        if self._fail:
            raise RuntimeError("fake recompute failure")
        return _FakeBatchResult(tags_written=len(segment_ids))


def _make_scheduler(
    svc: _FakeRecomputeService | None = None,
    *,
    interval_n: int = 5,
    debounce_ms: float = 500.0,
    tenant_id: str = "t1",
    recording_id: int = 42,
) -> tuple[StreamingTagScheduler, _FakeRecomputeService]:
    svc = svc or _FakeRecomputeService()
    sched = StreamingTagScheduler(
        svc,  # type: ignore[arg-type]
        interval_n=interval_n,
        debounce_ms=debounce_ms,
        tenant_id=tenant_id,
        recording_id=recording_id,
    )
    return sched, svc


# ============================================================
# Constructor validation
# ============================================================


class TestConstructor:
    def test_defaults(self) -> None:
        sched, _ = _make_scheduler()
        assert sched.tenant_id == "t1"
        assert sched.recording_id == 42
        assert sched.pending_count == 0
        assert sched.trigger_count_since_last == 0

    def test_invalid_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="interval_n"):
            StreamingTagScheduler(_FakeRecomputeService(), interval_n=0)  # type: ignore[arg-type]

    def test_invalid_debounce_raises(self) -> None:
        with pytest.raises(ValueError, match="debounce_ms"):
            StreamingTagScheduler(_FakeRecomputeService(), debounce_ms=-1.0)  # type: ignore[arg-type]


# ============================================================
# Interval trigger semantics
# ============================================================


class TestIntervalTrigger:
    @pytest.mark.asyncio
    async def test_below_threshold_no_trigger(self) -> None:
        sched, svc = _make_scheduler(interval_n=5)
        for i in range(4):
            result = await sched.on_segment_confirmed(i)
            assert result is None
        assert svc.calls == []
        assert sched.pending_count == 4

    @pytest.mark.asyncio
    async def test_five_segments_trigger_once(self) -> None:
        """AC: 5 confirmed segments → exactly one batch trigger."""
        sched, svc = _make_scheduler(interval_n=5)
        result = None
        for i in range(5):
            result = await sched.on_segment_confirmed(i)
        assert result is not None
        assert len(svc.calls) == 1
        assert svc.calls[0]["segment_ids"] == [0, 1, 2, 3, 4]
        assert sched.pending_count == 0
        assert sched.trigger_count_since_last == 0

    @pytest.mark.asyncio
    async def test_six_segments_trigger_one_accumulate_one(self) -> None:
        """AC: 6 segments → 1 trigger + 1 accumulated."""
        sched, svc = _make_scheduler(interval_n=5)
        result = None
        for i in range(6):
            result = await sched.on_segment_confirmed(i)
        assert result is None  # 6th segment did not trigger
        assert len(svc.calls) == 1
        assert sched.pending_count == 1
        assert sched.trigger_count_since_last == 1

    @pytest.mark.asyncio
    async def test_ten_segments_trigger_twice(self) -> None:
        sched, svc = _make_scheduler(interval_n=5, debounce_ms=0.0)
        for i in range(10):
            await sched.on_segment_confirmed(i)
        assert len(svc.calls) == 2
        assert svc.calls[1]["segment_ids"] == [5, 6, 7, 8, 9]

    @pytest.mark.asyncio
    async def test_interval_one_triggers_every_segment(self) -> None:
        sched, svc = _make_scheduler(interval_n=1, debounce_ms=0.0)
        for i in range(3):
            result = await sched.on_segment_confirmed(i)
            assert result is not None
        assert len(svc.calls) == 3

    @pytest.mark.asyncio
    async def test_result_carries_tenant_and_recording(self) -> None:
        sched, _ = _make_scheduler(interval_n=1, tenant_id="acme", recording_id=7)
        result = await sched.on_segment_confirmed(101)
        assert result is not None
        assert result.tenant_id == "acme"
        assert result.recording_id == 7
        assert result.segment_ids == [101]
        assert result.tags_written == 1
        assert result.error is None


# ============================================================
# Debounce
# ============================================================


class TestDebounce:
    @pytest.mark.asyncio
    async def test_burst_within_debounce_window_merged(self) -> None:
        """Two threshold crossings within debounce_ms → only one trigger."""
        sched, svc = _make_scheduler(interval_n=2, debounce_ms=60_000.0)
        for i in range(2):
            await sched.on_segment_confirmed(i)
        assert len(svc.calls) == 1
        # Next 2 segments hit the threshold again but inside debounce window.
        for i in range(2, 4):
            result = await sched.on_segment_confirmed(i)
            assert result is None
        assert len(svc.calls) == 1
        # Segments keep accumulating past the threshold.
        assert sched.pending_count == 2

    @pytest.mark.asyncio
    async def test_trigger_after_debounce_window_elapsed(self) -> None:
        sched, svc = _make_scheduler(interval_n=2, debounce_ms=50.0)
        for i in range(2):
            await sched.on_segment_confirmed(i)
        assert len(svc.calls) == 1
        await asyncio.sleep(0.08)  # > 50 ms debounce window
        for i in range(2, 4):
            await sched.on_segment_confirmed(i)
        assert len(svc.calls) == 2

    @pytest.mark.asyncio
    async def test_zero_debounce_always_triggers(self) -> None:
        sched, svc = _make_scheduler(interval_n=1, debounce_ms=0.0)
        for i in range(3):
            await sched.on_segment_confirmed(i)
        assert len(svc.calls) == 3

    @pytest.mark.asyncio
    async def test_debounce_pending_flushed_on_close(self) -> None:
        """Segments withheld by debounce are emitted on flush()."""
        sched, svc = _make_scheduler(interval_n=2, debounce_ms=60_000.0)
        for i in range(4):
            await sched.on_segment_confirmed(i)
        assert len(svc.calls) == 1
        flushed = await sched.flush()
        assert flushed is not None
        assert flushed.segment_ids == [2, 3]
        assert len(svc.calls) == 2


# ============================================================
# flush()
# ============================================================


class TestFlush:
    @pytest.mark.asyncio
    async def test_flush_empty_returns_none(self) -> None:
        sched, svc = _make_scheduler()
        assert await sched.flush() is None
        assert svc.calls == []

    @pytest.mark.asyncio
    async def test_flush_pending_segments(self) -> None:
        sched, _ = _make_scheduler(interval_n=5)
        for i in range(3):
            await sched.on_segment_confirmed(i)
        result = await sched.flush()
        assert result is not None
        assert result.segment_ids == [0, 1, 2]
        assert sched.pending_count == 0

    @pytest.mark.asyncio
    async def test_flush_after_trigger_no_pending(self) -> None:
        sched, svc = _make_scheduler(interval_n=2)
        for i in range(2):
            await sched.on_segment_confirmed(i)
        assert await sched.flush() is None
        assert len(svc.calls) == 1

    @pytest.mark.asyncio
    async def test_flush_ignores_debounce(self) -> None:
        """flush() triggers even within the debounce window."""
        sched, svc = _make_scheduler(interval_n=1, debounce_ms=60_000.0)
        await sched.on_segment_confirmed(0)
        await sched.on_segment_confirmed(1)  # debounced — accumulates
        result = await sched.flush()
        assert result is not None
        assert result.segment_ids == [1]
        assert len(svc.calls) == 2


# ============================================================
# Tenant isolation
# ============================================================


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_two_tenants_independent_counters(self) -> None:
        svc = _FakeRecomputeService()
        sched_a, _ = _make_scheduler(svc, interval_n=3, tenant_id="tenant-a", recording_id=1)
        sched_b, _ = _make_scheduler(svc, interval_n=3, tenant_id="tenant-b", recording_id=2)
        for i in range(2):
            await sched_a.on_segment_confirmed(i)
        for i in range(3):
            result_b = await sched_b.on_segment_confirmed(i)
        assert result_b is not None  # tenant-b triggered on its own count
        assert sched_a.pending_count == 2
        assert sched_b.pending_count == 0
        assert svc.calls[0]["tenant_id"] == "tenant-b"

    @pytest.mark.asyncio
    async def test_tenant_id_propagated_to_service(self) -> None:
        sched, svc = _make_scheduler(interval_n=1, tenant_id="tenant-x", recording_id=9)
        await sched.on_segment_confirmed(5)
        assert svc.calls[0]["tenant_id"] == "tenant-x"
        assert svc.calls[0]["recording_id"] == 9


# ============================================================
# Error resilience
# ============================================================


class TestErrorResilience:
    @pytest.mark.asyncio
    async def test_recompute_failure_returns_error_result(self) -> None:
        svc = _FakeRecomputeService(fail=True)
        sched, _ = _make_scheduler(svc, interval_n=1)
        result = await sched.on_segment_confirmed(0)
        assert result is not None
        assert result.error is not None
        assert "fake recompute failure" in result.error
        assert result.tags_written == 0

    @pytest.mark.asyncio
    async def test_state_reset_after_failure(self) -> None:
        """Failed batch still resets the accumulator (no unbounded growth)."""
        svc = _FakeRecomputeService(fail=True)
        sched, _ = _make_scheduler(svc, interval_n=2)
        await sched.on_segment_confirmed(0)
        await sched.on_segment_confirmed(1)
        assert sched.pending_count == 0
        assert sched.trigger_count_since_last == 0

    @pytest.mark.asyncio
    async def test_result_type_is_tag_batch_result(self) -> None:
        sched, _ = _make_scheduler(interval_n=1)
        result = await sched.on_segment_confirmed(0)
        assert isinstance(result, TagBatchResult)
