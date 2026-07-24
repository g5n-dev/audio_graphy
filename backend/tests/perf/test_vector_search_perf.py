"""Opt-in performance gates for the hot normalized vector-search path."""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from audio_graphy.storage.vector_index_cache import (
    NormalizedVectorIndex,
    PreallocatedNormalizedIndexBuilder,
    estimate_vector_load_peak_bytes,
    search_normalized_index,
)

RUN_PERF_TESTS = os.environ.get("RUN_PERF_TESTS") == "1"


@pytest.mark.perf
@pytest.mark.skipif(not RUN_PERF_TESTS, reason="set RUN_PERF_TESTS=1")
def test_hot_search_100k_vectors_p95_under_budget() -> None:
    """A cached 100k-row search must remain within the interactive SLA.

    The default CI dimension is 128 to keep the gate below 100 MiB. A release
    runner can set ``VECTOR_PERF_DIM=1024`` to exercise the production BGE
    dimension with the same row count and budget override.
    """

    row_count = 100_000
    dim = int(os.environ.get("VECTOR_PERF_DIM", "128"))
    budget_seconds = float(os.environ.get("VECTOR_PERF_P95_SECONDS", "0.2"))
    rounds = int(os.environ.get("VECTOR_PERF_ROUNDS", "20"))
    rng = np.random.default_rng(20260723)
    matrix = rng.standard_normal((row_count, dim), dtype=np.float32)
    matrix /= np.clip(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12, None)
    matrix.flags.writeable = False
    index = NormalizedVectorIndex(
        ids=tuple(range(row_count)),
        matrix=matrix,
        dim=dim,
    )
    query = tuple(float(value) for value in rng.standard_normal(dim))

    for _ in range(3):
        search_normalized_index(index, query, top_k=20)

    latencies: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        hits = search_normalized_index(index, query, top_k=20)
        latencies.append(time.perf_counter() - started)
        assert len(hits) == 20

    p95_seconds = float(np.percentile(latencies, 95))
    assert p95_seconds <= budget_seconds, (
        f"100k x {dim} hot vector query p95={p95_seconds:.4f}s exceeds {budget_seconds:.4f}s"
    )


@pytest.mark.perf
@pytest.mark.skipif(not RUN_PERF_TESTS, reason="set RUN_PERF_TESTS=1")
def test_cold_build_100k_vectors_stays_within_single_matrix_budget() -> None:
    """A streamed cold build retains one matrix and bounded batch memory."""

    row_count = 100_000
    dim = int(os.environ.get("VECTOR_PERF_DIM", "128"))
    batch_rows = int(os.environ.get("VECTOR_LOAD_BATCH_ROWS", "512"))
    memory_budget = int(os.environ.get("VECTOR_COLD_MEMORY_BYTES", str(80 * 1024 * 1024)))
    time_budget = float(os.environ.get("VECTOR_COLD_SECONDS", "8.0"))
    blob_bytes = dim * np.dtype(np.float32).itemsize
    estimated_peak = estimate_vector_load_peak_bytes(
        row_count=row_count,
        dim=dim,
        batch_rows=batch_rows,
        source_bytes=row_count * blob_bytes,
        max_blob_bytes=blob_bytes,
    )
    assert estimated_peak <= memory_budget

    vector = np.ones(dim, dtype=np.float32)
    blob = vector.tobytes()
    builder = PreallocatedNormalizedIndexBuilder(
        row_capacity=row_count,
        dim=dim,
        log_label="perf",
    )
    started = time.perf_counter()
    for offset in range(0, row_count, batch_rows):
        batch_end = min(offset + batch_rows, row_count)
        builder.add_rows((row_id, blob) for row_id in range(offset, batch_end))
    index = builder.finish()
    elapsed = time.perf_counter() - started

    assert index.matrix.shape == (row_count, dim)
    assert index.matrix_allocation_bytes == row_count * blob_bytes
    assert elapsed <= time_budget, (
        f"100k x {dim} streamed cold build={elapsed:.3f}s exceeds {time_budget:.3f}s"
    )
