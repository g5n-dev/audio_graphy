"""FunASR per-tenant WebSocket connection pool (Q1 decision).

M8 Phase 4 (WS-1 / T3). Per-tenant isolation over single-conn multiplexing.

Rationale (Q1, see docs/m8-architecture.md §19.1):

    | Dimension                | per-tenant pool (selected) | single-conn (rejected)   |
    |--------------------------|----------------------------|--------------------------|
    | Failure blast radius     | Single tenant              | All tenants              |
    | Resource cost            | 8 × N tenants × ~5MB       | 1 × ~5MB                 |
    | Hotwords injection       | Per-tenant at connect      | Per-call switching (no)  |
    | funASR concurrency (≤20) | Pool=8 leaves buffer       | Single conn = serial     |

Implementation:
    - Lazy pool init per tenant (first ``acquire()`` creates the pool).
    - ``asyncio.Semaphore`` caps concurrent in-use connections per tenant.
    - On release, healthy adapters are returned to the free-list; errored
      adapters are closed and discarded (so the next acquire rebuilds).
    - Pool is process-local (no cross-process sharing). For multi-process
      deployments, scale by adding funASR replicas, not by sharing pools.

Lifecycle:
    pool = FunASRConnectionPool(ws_url="ws://funasr:10095", pool_size_per_tenant=8)
    adapter = await pool.acquire(tenant_id, session_id, hotwords)
    try:
        delta = await adapter.push_pcm(pcm, seq=...)
    finally:
        await pool.release(adapter)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from audio_graphy.adapters.real.streaming_funasr import (
    _DEFAULT_CHUNK_INTERVAL,
    _DEFAULT_CHUNK_SIZE,
    _DEFAULT_CONNECT_TIMEOUT_SEC,
    _DEFAULT_FINALIZE_TIMEOUT_SEC,
    _DEFAULT_MODEL,
    _DEFAULT_PUSH_TIMEOUT_SEC,
    StreamingFunASRAdapter,
)

logger = logging.getLogger(__name__)


@dataclass
class _TenantPool:
    """One tenant's connection pool bookkeeping.

    Attributes:
        free: Idle adapters available for reuse.
        in_use: Adapters currently handed out (for diagnostics only).
        semaphore: Caps concurrent usage at ``pool_size_per_tenant``.
    """

    free: list[StreamingFunASRAdapter] = field(default_factory=list)
    in_use: set[StreamingFunASRAdapter] = field(default_factory=set)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(8))


class FunASRConnectionPool:
    """Per-tenant funASR WebSocket connection pool (Q1).

    Args:
        ws_url: funASR server URL (e.g. ``"ws://funasr:10095"``).
        pool_size_per_tenant: Max concurrent in-use connections per tenant
            (default 8 per Q1 decision).
        max_wait_sec: Acquire timeout (default 30s). Exceeding raises
            ``StreamingASRConnectTimeout``.
        model: funASR model name (L2 locked ``paraformer-zh-streaming``).
        chunk_size: funASR chunk_size (L2 locked ``[5,10,5]``).
        chunk_interval: funASR chunk_interval (default 10).
        connect_timeout_sec: Per-connection handshake timeout.
        push_timeout_sec: Per-push response timeout.
        finalize_timeout_sec: Finalize drain timeout.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        pool_size_per_tenant: int = 8,
        max_wait_sec: float = 30.0,
        model: str = _DEFAULT_MODEL,
        chunk_size: tuple[int, int, int] = _DEFAULT_CHUNK_SIZE,
        chunk_interval: int = _DEFAULT_CHUNK_INTERVAL,
        connect_timeout_sec: float = _DEFAULT_CONNECT_TIMEOUT_SEC,
        push_timeout_sec: float = _DEFAULT_PUSH_TIMEOUT_SEC,
        finalize_timeout_sec: float = _DEFAULT_FINALIZE_TIMEOUT_SEC,
    ) -> None:
        self._ws_url = ws_url.rstrip("/")
        self._pool_size = pool_size_per_tenant
        self._max_wait_sec = max_wait_sec
        self._model = model
        self._chunk_size = chunk_size
        self._chunk_interval = chunk_interval
        self._connect_timeout = connect_timeout_sec
        self._push_timeout = push_timeout_sec
        self._finalize_timeout = finalize_timeout_sec

        # Per-tenant state. Created lazily on first acquire for that tenant.
        self._pools: dict[str, _TenantPool] = {}
        self._pools_lock = asyncio.Lock()

    @property
    def pool_size_per_tenant(self) -> int:
        return self._pool_size

    def tenants_known(self) -> list[str]:
        """Return the list of tenants with an initialised pool (diagnostics)."""
        return list(self._pools.keys())

    def free_count(self, tenant_id: str) -> int:
        """Return the count of idle adapters for a tenant (diagnostics)."""
        pool = self._pools.get(tenant_id)
        return len(pool.free) if pool is not None else 0

    async def acquire(
        self,
        tenant_id: str,
        session_id: str,
        hotwords: Sequence[str],
    ) -> StreamingFunASRAdapter:
        """Acquire a connected adapter from the tenant's pool.

        If the free-list is empty, a fresh adapter is created and ``connect()``
        is called. If the tenant is at capacity (``pool_size_per_tenant`` in
        use), the call blocks until one is released or ``max_wait_sec``.

        Raises:
            StreamingASRConnectTimeout: ``max_wait_sec`` exceeded.
            Any ``StreamingASR*Error`` raised by ``connect()``.
        """
        pool = await self._ensure_pool(tenant_id)

        try:
            await asyncio.wait_for(pool.semaphore.acquire(), timeout=self._max_wait_sec)
        except TimeoutError as exc:
            from audio_graphy.adapters.exceptions import StreamingASRConnectTimeout

            raise StreamingASRConnectTimeout(
                f"funASR pool exhausted for tenant={tenant_id} after {self._max_wait_sec}s",
            ) from exc

        # Try to reuse a free adapter; fall back to creating a new one.
        adapter: StreamingFunASRAdapter | None = None
        while pool.free:
            candidate = pool.free.pop()
            # Candidate is already connected; if its WS died while idle, skip.
            if await self._is_alive(candidate):
                adapter = candidate
                break
            # Discard dead adapter.
            await self._safe_aclose(candidate)

        if adapter is None:
            adapter = StreamingFunASRAdapter(
                ws_url=self._ws_url,
                model=self._model,
                chunk_size=self._chunk_size,
                chunk_interval=self._chunk_interval,
                connect_timeout_sec=self._connect_timeout,
                push_timeout_sec=self._push_timeout,
                finalize_timeout_sec=self._finalize_timeout,
                tenant_id=tenant_id,
            )
            await adapter.connect(
                session_id=session_id,
                tenant_id=tenant_id,
                hotwords=hotwords,
            )

        pool.in_use.add(adapter)
        return adapter

    async def release(self, adapter: StreamingFunASRAdapter) -> None:
        """Return adapter to its pool (or close if the pool is full / errored)."""
        tenant_id = adapter._tenant_id
        pool = self._pools.get(tenant_id)
        if pool is None:
            # Pool was reset between acquire and release — close the adapter.
            await self._safe_aclose(adapter)
            return

        pool.in_use.discard(adapter)
        with contextlib.suppress(ValueError):
            # Semaphore already at initial value (release without acquire) — ignore.
            pool.semaphore.release()

        # Return to free-list only if healthy and under capacity.
        if await self._is_alive(adapter):
            pool.free.append(adapter)
        else:
            await self._safe_aclose(adapter)

    async def close_all(self) -> None:
        """Close every adapter in every tenant pool (shutdown hook)."""
        async with self._pools_lock:
            for _tenant_id, pool in self._pools.items():
                for adapter in pool.free:
                    await self._safe_aclose(adapter)
                for adapter in pool.in_use:
                    await self._safe_aclose(adapter)
                pool.free.clear()
                pool.in_use.clear()
            self._pools.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _ensure_pool(self, tenant_id: str) -> _TenantPool:
        """Get-or-create the pool for one tenant."""
        # Fast path: already initialised.
        pool = self._pools.get(tenant_id)
        if pool is not None:
            return pool

        async with self._pools_lock:
            # Re-check under lock.
            pool = self._pools.get(tenant_id)
            if pool is None:
                pool = _TenantPool(
                    semaphore=asyncio.Semaphore(self._pool_size),
                )
                self._pools[tenant_id] = pool
                logger.debug(
                    "funASR pool created tenant=%s size=%d",
                    tenant_id,
                    self._pool_size,
                )
            return pool

    @staticmethod
    async def _is_alive(adapter: StreamingFunASRAdapter) -> bool:
        """Cheap liveness probe — checks the closed flag and ws reference."""
        if getattr(adapter, "_closed", False):
            return False
        return getattr(adapter, "_ws", None) is not None

    @staticmethod
    async def _safe_aclose(adapter: StreamingFunASRAdapter) -> None:
        """Best-effort close — swallows all errors."""
        try:
            await adapter.aclose()
        except Exception as exc:
            logger.debug("funASR pool aclose error (ignored): %s", exc)
