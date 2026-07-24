"""Reusable normalized vector indexes with tenant-scoped LRU caching.

The MySQL vector stores persist the source of truth as float32 BLOBs.  This
module keeps a bounded, short-lived normalized matrix in process so a hot
query performs one matrix-vector multiplication instead of reloading and
normalizing every tenant row on every request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.types import VectorSearchHit

logger = logging.getLogger(__name__)

type VectorId = str | int
type VectorRow = tuple[VectorId, bytes]
type VectorCacheKey = tuple[str, str]
type VectorLoadResult = Sequence[VectorRow] | NormalizedVectorIndex
type VectorLoader = Callable[[], Awaitable[VectorLoadResult]]

_FLOAT32_BYTES = np.dtype(np.float32).itemsize
_CACHE_ID_ESTIMATE_BYTES = 16
_LOAD_ID_ESTIMATE_BYTES = 64
_LOAD_ID_TRANSITION_BYTES = 8
_LOAD_ROW_ESTIMATE_BYTES = 256


class VectorIndexBudgetError(RuntimeError):
    """A tenant index cannot be loaded within its configured resource budget."""


@dataclass(frozen=True, slots=True)
class NormalizedVectorIndex:
    """Immutable IDs plus a read-only row-normalized float32 matrix."""

    ids: tuple[VectorId, ...]
    matrix: np.ndarray
    dim: int
    # A sliced matrix retains its full preallocated base array. Keep that real
    # allocation visible to cache accounting instead of using the view's nbytes.
    matrix_allocation_bytes: int | None = None
    id_allocation_bytes: int | None = None

    @property
    def retained_size_bytes(self) -> int:
        """Estimated bytes retained while this index is cached."""

        matrix_bytes = (
            int(self.matrix.nbytes)
            if self.matrix_allocation_bytes is None
            else self.matrix_allocation_bytes
        )
        id_bytes = (
            len(self.ids) * _CACHE_ID_ESTIMATE_BYTES
            if self.id_allocation_bytes is None
            else self.id_allocation_bytes
        )
        return matrix_bytes + id_bytes


@dataclass(slots=True)
class VectorCacheStats:
    """Process-local cache counters used by metrics and performance tests."""

    hits: int = 0
    misses: int = 0
    loads: int = 0
    evictions: int = 0
    invalidations: int = 0
    oversized_skips: int = 0


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    index: NormalizedVectorIndex
    loaded_at: float
    size_bytes: int


class PreallocatedNormalizedIndexBuilder:
    """Build one normalized float32 matrix without stack-sized temporaries."""

    def __init__(
        self,
        *,
        row_capacity: int,
        dim: int,
        log_label: str,
        id_bytes_per_row: int | None = None,
    ) -> None:
        if row_capacity < 0:
            raise ValueError("row_capacity must be non-negative")
        if dim < 1:
            raise ValueError("dim must be at least 1")
        if id_bytes_per_row is not None and id_bytes_per_row < 1:
            raise ValueError("id_bytes_per_row must be at least 1 when provided")
        self._row_capacity = row_capacity
        self._dim = dim
        self._log_label = log_label
        self._id_bytes_per_row = id_bytes_per_row
        self._matrix = np.empty((row_capacity, dim), dtype=np.float32)
        self._ids: list[VectorId] = []
        self._seen_rows = 0
        self._valid_rows = 0

    @property
    def seen_rows(self) -> int:
        """Number of source rows consumed, including corrupt rows."""

        return self._seen_rows

    def add_rows(self, rows: Iterable[Any]) -> int:
        """Decode and append a bounded batch, returning the valid row count."""

        added = 0
        for row in rows:
            if self._seen_rows >= self._row_capacity:
                raise VectorIndexBudgetError(
                    "vector row count changed during streaming and exceeded "
                    f"preflight capacity {self._row_capacity}"
                )
            self._seen_rows += 1

            try:
                row_id, blob = row
            except (TypeError, ValueError) as exc:
                self._warn_corrupt("<unknown>", f"invalid projected row: {exc}")
                continue
            if not isinstance(row_id, (str, int)):
                self._warn_corrupt(row_id, "id must be str or int")
                continue
            if not isinstance(blob, (bytes, bytearray, memoryview)):
                self._warn_corrupt(row_id, "embedding must be bytes-like")
                continue

            try:
                vector = np.frombuffer(blob, dtype=np.float32)
            except (TypeError, ValueError) as exc:
                self._warn_corrupt(row_id, str(exc))
                continue
            if vector.shape != (self._dim,):
                self._warn_corrupt(
                    row_id,
                    f"dimension mismatch: expected {self._dim}, got {vector.shape[0]}",
                )
                continue
            if not bool(np.isfinite(vector).all()):
                self._warn_corrupt(row_id, "embedding contains NaN or infinity")
                continue

            squared_norm = float(np.dot(vector, vector))
            norm = float(np.sqrt(squared_norm))
            if not np.isfinite(norm):
                self._warn_corrupt(row_id, "embedding norm is not finite")
                continue

            target = self._matrix[self._valid_rows]
            np.copyto(target, vector, casting="no")
            np.divide(target, max(norm, 1e-12), out=target)
            self._ids.append(row_id)
            self._valid_rows += 1
            added += 1

        return added

    def finish(self) -> NormalizedVectorIndex:
        """Return a read-only view while retaining exact allocation accounting."""

        matrix = self._matrix[: self._valid_rows]
        matrix.flags.writeable = False
        return NormalizedVectorIndex(
            ids=tuple(self._ids),
            matrix=matrix,
            dim=self._dim,
            matrix_allocation_bytes=int(self._matrix.nbytes),
            id_allocation_bytes=(
                None
                if self._id_bytes_per_row is None
                else self._valid_rows * self._id_bytes_per_row
            ),
        )

    def _warn_corrupt(self, row_id: object, reason: str) -> None:
        logger.warning(
            "Skipping corrupt %s vector id=%s: %s",
            self._log_label,
            row_id,
            reason,
        )


def estimate_vector_load_peak_bytes(
    *,
    row_count: int,
    dim: int,
    batch_rows: int,
    source_bytes: int,
    max_blob_bytes: int,
    max_id_bytes_per_row: int = 0,
) -> int:
    """Conservative process-memory estimate for one streamed cold load."""

    if row_count <= 0:
        return 0
    batch_count = min(row_count, batch_rows)
    matrix_bytes = row_count * dim * _FLOAT32_BYTES
    retained_ids_bytes = row_count * (_LOAD_ID_ESTIMATE_BYTES + max_id_bytes_per_row)
    id_tuple_transition_bytes = row_count * _LOAD_ID_TRANSITION_BYTES
    batch_blob_bytes = min(source_bytes, batch_count * max_blob_bytes)
    batch_row_bytes = batch_count * _LOAD_ROW_ESTIMATE_BYTES
    normalization_scratch_bytes = dim * _FLOAT32_BYTES
    return (
        matrix_bytes
        + retained_ids_bytes
        + id_tuple_transition_bytes
        + batch_blob_bytes
        + batch_row_bytes
        + normalization_scratch_bytes
    )


def validate_vector_load_options(
    *,
    batch_rows: int,
    max_rows: int,
    max_source_bytes: int,
    max_memory_bytes: int,
) -> None:
    """Reject invalid resource limits at store construction time."""

    values = {
        "load_batch_rows": batch_rows,
        "load_max_rows": max_rows,
        "load_max_source_bytes": max_source_bytes,
        "load_max_memory_bytes": max_memory_bytes,
    }
    for name, value in values.items():
        if value < 1:
            raise ValueError(f"{name} must be at least 1")


async def stream_normalized_index(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    id_column: Any,
    blob_column: Any,
    tenant_column: Any,
    tenant_id: str,
    dim: int,
    batch_rows: int,
    max_rows: int,
    max_source_bytes: int,
    max_memory_bytes: int,
    log_label: str,
    max_id_bytes_per_row: int = 0,
) -> NormalizedVectorIndex:
    """Preflight limits, server-stream projected BLOB rows, and normalize once."""

    validate_vector_load_options(
        batch_rows=batch_rows,
        max_rows=max_rows,
        max_source_bytes=max_source_bytes,
        max_memory_bytes=max_memory_bytes,
    )
    if dim < 1:
        raise ValueError("dim must be at least 1")
    if max_id_bytes_per_row < 0:
        raise ValueError("max_id_bytes_per_row must be non-negative")

    async with session_factory() as session:
        length = func.length(blob_column)
        preflight_stmt = select(
            func.count(),
            func.coalesce(func.sum(length), 0),
            func.coalesce(func.max(length), 0),
        ).where(tenant_column == tenant_id)
        stats = (await session.execute(preflight_stmt)).one()
        row_count = int(stats[0] or 0)
        source_bytes = int(stats[1] or 0)
        max_blob_bytes = int(stats[2] or 0)

        if row_count < 0 or source_bytes < 0 or max_blob_bytes < 0:
            raise VectorIndexBudgetError("vector preflight returned invalid negative statistics")
        if row_count > max_rows:
            raise VectorIndexBudgetError(
                f"vector row count {row_count} exceeds configured maximum {max_rows}"
            )
        if source_bytes > max_source_bytes:
            raise VectorIndexBudgetError(
                f"vector source bytes {source_bytes} exceed configured maximum {max_source_bytes}"
            )

        estimated_peak = estimate_vector_load_peak_bytes(
            row_count=row_count,
            dim=dim,
            batch_rows=batch_rows,
            source_bytes=source_bytes,
            max_blob_bytes=max_blob_bytes,
            max_id_bytes_per_row=max_id_bytes_per_row,
        )
        if estimated_peak > max_memory_bytes:
            raise VectorIndexBudgetError(
                "vector estimated peak memory "
                f"{estimated_peak} exceeds configured maximum {max_memory_bytes}"
            )

        builder = PreallocatedNormalizedIndexBuilder(
            row_capacity=row_count,
            dim=dim,
            log_label=log_label,
            id_bytes_per_row=_LOAD_ID_ESTIMATE_BYTES + max_id_bytes_per_row,
        )
        if row_count == 0:
            return builder.finish()

        stream_stmt = (
            select(id_column, blob_column)
            .where(tenant_column == tenant_id)
            .execution_options(yield_per=batch_rows)
        )
        result = await session.stream(stream_stmt)
        streamed_source_bytes = 0
        async for partition in result.partitions(batch_rows):
            streamed_source_bytes += sum(
                len(row[1])
                for row in partition
                if len(row) >= 2 and isinstance(row[1], (bytes, bytearray, memoryview))
            )
            if streamed_source_bytes > max_source_bytes:
                raise VectorIndexBudgetError(
                    "vector source bytes changed during streaming and exceeded "
                    f"configured maximum {max_source_bytes}"
                )
            await asyncio.to_thread(builder.add_rows, partition)

        return builder.finish()


def build_normalized_index(
    rows: Sequence[VectorRow],
    *,
    dim: int,
    log_label: str,
) -> NormalizedVectorIndex:
    """Decode, validate and normalize stored vectors once.

    Corrupt rows are isolated rather than making the complete tenant index
    unavailable.  The returned matrix is marked read-only so cached data
    cannot be accidentally mutated by a caller.
    """

    builder = PreallocatedNormalizedIndexBuilder(
        row_capacity=len(rows),
        dim=dim,
        log_label=log_label,
    )
    builder.add_rows(rows)
    return builder.finish()


def search_normalized_index(
    index: NormalizedVectorIndex,
    query_vec: tuple[float, ...],
    *,
    top_k: int,
) -> list[VectorSearchHit]:
    """Search an already-normalized matrix and return descending cosine hits."""

    if top_k <= 0:
        return []

    query = np.asarray(query_vec, dtype=np.float32)
    if query.shape != (index.dim,):
        raise ValueError(
            f"Query vector dimension mismatch: expected {index.dim}, got "
            f"{query.shape[0] if query.ndim == 1 else query.shape}"
        )
    if not index.ids:
        return []

    query_norm = max(float(np.linalg.norm(query)), 1e-12)
    scores = index.matrix @ (query / query_norm)

    k = min(top_k, len(index.ids))
    top_k_idx = np.argpartition(scores, -k)[-k:]
    top_k_sorted = top_k_idx[np.argsort(scores[top_k_idx])[::-1]]
    return [
        VectorSearchHit(id=index.ids[int(idx)], score=float(scores[int(idx)]))
        for idx in top_k_sorted
    ]


class TenantVectorIndexCache:
    """Bounded TTL/LRU cache with one loader in flight per tenant and channel."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 60.0,
        max_entries: int = 32,
        max_bytes: int = 512 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._clock = clock
        self._entries: OrderedDict[VectorCacheKey, _CacheEntry] = OrderedDict()
        self._locks: dict[VectorCacheKey, asyncio.Lock] = {}
        self._generations: dict[VectorCacheKey, int] = {}
        self._cached_bytes = 0
        self.stats = VectorCacheStats()

    @property
    def entry_count(self) -> int:
        """Current number of cached tenant/channel indexes."""

        return len(self._entries)

    @property
    def cached_bytes(self) -> int:
        """Estimated bytes retained by normalized matrices and their ID tuples."""

        return self._cached_bytes

    @property
    def max_bytes(self) -> int:
        """Configured process-local cache memory budget."""

        return self._max_bytes

    async def get_or_load(
        self,
        key: VectorCacheKey,
        loader: VectorLoader,
        *,
        dim: int,
    ) -> NormalizedVectorIndex:
        """Return a fresh index, loading it exactly once on concurrent misses."""

        entry = self._get_fresh(key)
        if entry is not None:
            self.stats.hits += 1
            return entry.index

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._get_fresh(key)
            if entry is not None:
                self.stats.hits += 1
                return entry.index

            while True:
                generation = self._generations.get(key, 0)
                self.stats.misses += 1
                loaded = await loader()
                if isinstance(loaded, NormalizedVectorIndex):
                    index = loaded
                    _validate_prebuilt_index(index, expected_dim=dim)
                else:
                    index = await asyncio.to_thread(
                        build_normalized_index,
                        loaded,
                        dim=dim,
                        log_label=f"{key[0]}/{key[1]}",
                    )
                if generation != self._generations.get(key, 0):
                    continue

                self.stats.loads += 1
                size_bytes = index.retained_size_bytes
                if size_bytes > self._max_bytes:
                    self.stats.oversized_skips += 1
                    return index

                previous = self._entries.pop(key, None)
                if previous is not None:
                    self._cached_bytes -= previous.size_bytes
                self._entries[key] = _CacheEntry(
                    index=index,
                    loaded_at=self._clock(),
                    size_bytes=size_bytes,
                )
                self._cached_bytes += size_bytes
                self._entries.move_to_end(key)
                self._evict_over_capacity()
                return index

    def invalidate(self, key: VectorCacheKey) -> None:
        """Invalidate one tenant/channel after a committed write."""

        self._remove_entry(key)
        self._generations[key] = self._generations.get(key, 0) + 1
        self.stats.invalidations += 1

    def clear(self) -> None:
        """Drop all cached indexes, for lifecycle hooks and tests."""

        keys = self._entries.keys() | self._locks.keys()
        for key in keys:
            self._generations[key] = self._generations.get(key, 0) + 1
        self._entries.clear()
        self._cached_bytes = 0

    def _get_fresh(self, key: VectorCacheKey) -> _CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry.loaded_at >= self._ttl_seconds:
            self._remove_entry(key)
            return None
        self._entries.move_to_end(key)
        return entry

    def _evict_over_capacity(self) -> None:
        while len(self._entries) > self._max_entries or self._cached_bytes > self._max_bytes:
            evicted_key, entry = self._entries.popitem(last=False)
            self._cached_bytes -= entry.size_bytes
            self.stats.evictions += 1
            lock = self._locks.get(evicted_key)
            if lock is not None and not lock.locked():
                self._locks.pop(evicted_key, None)
                self._generations.pop(evicted_key, None)

    def _remove_entry(self, key: VectorCacheKey) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._cached_bytes -= entry.size_bytes


def _validate_prebuilt_index(
    index: NormalizedVectorIndex,
    *,
    expected_dim: int,
) -> None:
    if index.dim != expected_dim:
        raise ValueError(
            f"Loaded vector index dimension mismatch: expected {expected_dim}, got {index.dim}"
        )
    if index.matrix.dtype != np.float32:
        raise ValueError(f"Loaded vector index dtype must be float32, got {index.matrix.dtype}")
    if index.matrix.ndim != 2 or index.matrix.shape != (len(index.ids), expected_dim):
        raise ValueError(
            "Loaded vector index shape mismatch: "
            f"expected {(len(index.ids), expected_dim)}, got {index.matrix.shape}"
        )
    index.matrix.flags.writeable = False


__all__ = [
    "NormalizedVectorIndex",
    "PreallocatedNormalizedIndexBuilder",
    "TenantVectorIndexCache",
    "VectorCacheStats",
    "VectorIndexBudgetError",
    "VectorLoadResult",
    "build_normalized_index",
    "estimate_vector_load_peak_bytes",
    "search_normalized_index",
    "stream_normalized_index",
    "validate_vector_load_options",
]
