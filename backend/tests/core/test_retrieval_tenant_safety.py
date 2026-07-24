"""Tenant and identity regression tests for chunk/audio reverse lookup."""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pytest

from audio_graphy.core.retrieval import ThreeChannelRetriever
from audio_graphy.storage.mysql_audio_vector import MySQLAudioVectorStore


def _blob(values: tuple[float, ...]) -> bytes:
    return np.asarray(values, dtype=np.float32).tobytes()


@pytest.mark.unit
async def test_audio_search_resolves_vector_identity_with_tenant_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MySQLAudioVectorStore(cast(Any, None), dim=2)
    loads = 0
    metadata_queries = 0

    async def load_vectors(_tenant_id: str) -> list[tuple[int, bytes]]:
        nonlocal loads
        loads += 1
        return [(701, _blob((1.0, 0.0)))]

    async def load_metadata(
        tenant_id: str,
        vector_ids: list[int],
    ) -> dict[int, tuple[int, int, int | None]]:
        nonlocal metadata_queries
        metadata_queries += 1
        assert tenant_id == "tenant-a"
        assert vector_ids == [701]
        return {701: (41, 9, 88)}

    monkeypatch.setattr(store, "_load_vectors", load_vectors)
    monkeypatch.setattr(store, "_load_hit_metadata", load_metadata)

    first = await store.search_audio("tenant-a", (1.0, 0.0), top_k=1)
    second = await store.search_audio("tenant-a", (1.0, 0.0), top_k=1)

    assert loads == 1
    assert metadata_queries == 2
    assert first == second
    assert first[0].vector_id == 701
    assert first[0].recording_id == 41
    assert first[0].segment_id == 9
    assert first[0].chunk_id == 88
    assert first[0].score == pytest.approx(1.0)


class _EmptyResult:
    def __iter__(self) -> Any:
        return iter(())


class _CapturingSession:
    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> _EmptyResult:
        self.statement = statement
        return _EmptyResult()


class _SessionContext:
    def __init__(self, session: _CapturingSession) -> None:
        self._session = session

    async def __aenter__(self) -> _CapturingSession:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.unit
async def test_mysql_chunk_reverse_lookup_requires_tenant_on_both_tables() -> None:
    session = _CapturingSession()
    retriever = ThreeChannelRetriever.__new__(ThreeChannelRetriever)
    retriever._session_factory = lambda: _SessionContext(session)  # type: ignore[assignment]

    result = await retriever._lookup_chunks_mysql([7], tenant_id="tenant-a")

    assert result == {}
    rendered = str(session.statement)
    assert "chunks.tenant_id" in rendered
    assert "recordings.tenant_id" in rendered
    assert list(session.statement.compile().params.values()).count("tenant-a") == 2
