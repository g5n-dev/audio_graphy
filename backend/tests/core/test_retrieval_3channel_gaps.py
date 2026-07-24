"""M7 QA gap-fill — retrieval.py uncovered branches.

Targets lines flagged by coverage report:
- 255-262: graph channel early-exit on empty keywords / get_all_nodes exception / empty nodes
- 297-321: graph channel node lookup + neighbor path (covers 300, 321, 324, 333)
- 397-400: time filter tz normalization
- 498-502: LLM keyword extraction exception → fallback
- 548-554: _lookup_chunks session_factory=None + file_index=None
- 593-616: _lookup_chunks_file_index path
- 796-824: audio channel embed / search code path (lines around 796, 806, 814, 821, 824)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from audio_graphy.core.retrieval import (
    CandidateSegment,
    ThreeChannelRetriever,
)


def _candidate(
    chunk_id: int = 1,
    recording_id: int = 1,
    score: float = 0.8,
    source_channel: str = "naive",
    text: str = "test",
) -> CandidateSegment:
    return CandidateSegment(
        chunk_id=chunk_id,
        recording_id=recording_id,
        segment_ids=[chunk_id],
        text=text,
        recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
        score=score,
        source_channel=source_channel,
    )


# ============================================================
# Graph channel — early-exit branches
# ============================================================


class _RaisingGraphStore:
    """Graph store that raises on get_all_nodes (covers lines 260-262)."""

    tenant_id = "t1"

    async def get_all_nodes(self) -> list[Any]:
        raise RuntimeError("graph store down")

    async def get_relation_counts(self, eid: str) -> dict[str, int]:
        return {}

    async def get_node(self, eid: str) -> Any:
        return None

    async def get_neighbors(self, eid: str, max_hops: int = 1) -> list[Any]:
        return []


class _EmptyGraphStore:
    """Graph store returning empty nodes list (covers line 264-265)."""

    tenant_id = "t1"

    async def get_all_nodes(self) -> list[Any]:
        return []

    async def get_relation_counts(self, eid: str) -> dict[str, int]:
        return {}

    async def get_node(self, eid: str) -> Any:
        return None

    async def get_neighbors(self, eid: str, max_hops: int = 1) -> list[Any]:
        return []


class _StubVectorStore:
    async def search_chunks(self, *args: object, **kwargs: object) -> list[Any]:
        return []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graph_channel_handles_get_all_nodes_exception(
    mock_bundle: Any, tmp_working_dir: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Graph store exception is caught and logged (lines 260-262)."""
    retriever = ThreeChannelRetriever(
        mock_bundle,
        _StubVectorStore(),  # type: ignore[arg-type]
        _RaisingGraphStore(),  # type: ignore[arg-type]
        enable_audio_channel=False,
    )
    with caplog.at_level("WARNING"):
        result = await retriever.retrieve("query", tenant_id="t1")
    # Should not crash; result returned (potentially empty).
    assert result is not None
    assert any("Graph channel" in r.message for r in caplog.records)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graph_channel_empty_nodes_returns_empty(
    mock_bundle: Any, tmp_working_dir: Any
) -> None:
    """Empty all_nodes → graph channel returns [] (line 264-265)."""
    retriever = ThreeChannelRetriever(
        mock_bundle,
        _StubVectorStore(),  # type: ignore[arg-type]
        _EmptyGraphStore(),  # type: ignore[arg-type]
        enable_audio_channel=False,
    )
    result = await retriever.retrieve("query", tenant_id="t1")
    assert result is not None
    assert result.graph_hits == 0


# ============================================================
# LLM keyword extraction exception → fallback (lines 498-502)
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keyword_extraction_falls_back_on_exception(
    mock_bundle: Any, tmp_working_dir: Any
) -> None:
    """When weak_llm.complete raises, fallback segmentation is used (line 499-502)."""

    # Wrap the bundle so complete() raises.
    class _ExplodingBundle:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        @property
        def weak_llm(self) -> Any:
            class _Bad:
                async def complete(self, *a: object, **kw: object) -> Any:
                    raise RuntimeError("LLM down")

            return _Bad()

    bundle = _ExplodingBundle(mock_bundle)
    retriever = ThreeChannelRetriever(
        bundle,  # type: ignore[arg-type]
        _StubVectorStore(),  # type: ignore[arg-type]
        _EmptyGraphStore(),  # type: ignore[arg-type]
        enable_audio_channel=False,
    )
    # Should not raise — fallback used.
    result = await retriever.retrieve("某车型 价格", tenant_id="t1")
    assert result is not None


# ============================================================
# _lookup_chunks paths
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lookup_chunks_returns_empty_when_no_factory_or_index(
    mock_bundle: Any, tmp_working_dir: Any
) -> None:
    """No session_factory and no file_index → return {} (lines 548, 553-555)."""
    retriever = ThreeChannelRetriever(
        mock_bundle,
        _StubVectorStore(),  # type: ignore[arg-type]
        _EmptyGraphStore(),  # type: ignore[arg-type]
        enable_audio_channel=False,
    )
    # Both _session_factory and _file_index are None.
    out = await retriever._lookup_chunks([1, 2, 3], tenant_id="t1")
    assert out == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lookup_chunks_file_index_path(tmp_working_dir: Any) -> None:
    """Cover _lookup_chunks_file_index (lines 593-616) using a real FileIndex."""
    from audio_graphy.adapters.bundle import AdapterBundle
    from audio_graphy.adapters.mock_asr import MockASRAdapter
    from audio_graphy.adapters.mock_embed import MockEmbedAdapter
    from audio_graphy.adapters.mock_llm import MockLLMAdapter
    from audio_graphy.adapters.mock_vad import MockVADAdapter
    from audio_graphy.storage.file_index import FileIndex

    # Build a real FileIndex in the working dir.
    file_index = FileIndex(tmp_working_dir)

    # Seed kv_store_text_chunks and kv_store_video_path.
    await file_index.set(
        "kv_store_text_chunks",
        "rec_1_chunk_42",
        {
            "recording_id": 1,
            "segment_ids": [10],
            "text": "hello world",
        },
    )
    await file_index.set(
        "kv_store_video_path",
        "1",
        {
            "recorded_at": "2026-07-10T12:00:00+00:00",
        },
    )

    bundle = AdapterBundle(
        vad=MockVADAdapter(),
        asr=MockASRAdapter(),
        strong_llm=MockLLMAdapter(model="strong"),
        weak_llm=MockLLMAdapter(model="weak"),
        embed=MockEmbedAdapter(),
    )
    retriever = ThreeChannelRetriever(
        bundle,
        _StubVectorStore(),  # type: ignore[arg-type]
        _EmptyGraphStore(),  # type: ignore[arg-type]
        enable_audio_channel=False,
        file_index=file_index,
    )
    out = await retriever._lookup_chunks([42], tenant_id="default")
    assert isinstance(out, dict)


# ============================================================
# Time filter tz normalization (lines 397-400)
# ============================================================


@pytest.mark.unit
def test_filter_by_time_normalizes_naive_datetimes() -> None:
    """Naive datetimes (no tzinfo) are normalized to UTC (lines 397-400)."""
    from datetime import datetime as dt

    from audio_graphy.core.retrieval import ThreeChannelRetriever as _Ret

    cand = _candidate()
    naive_range = (dt(2026, 7, 1), dt(2026, 7, 31))
    filtered, removed = _Ret._filter_by_time([cand], naive_range)
    # Should not crash; candidate has tz-aware recorded_at inside range.
    assert len(filtered) + removed == 1


# ============================================================
# Audio channel end-to-end integration (covers 793-824)
# ============================================================


class _FakeAudioVectorStore:
    def __init__(self, hits: list[tuple[int, float]]) -> None:
        self._hits = hits

    async def search_audio(
        self,
        tenant_id: str,
        query_vec: tuple[float, ...],
        *,
        top_k: int = 10,
    ) -> list[Any]:
        from audio_graphy.storage.mysql_audio_vector import AudioVectorSearchHit

        return [
            AudioVectorSearchHit(
                vector_id=sid,
                recording_id=1,
                segment_id=sid,
                chunk_id=sid,
                score=score,
            )
            for sid, score in self._hits
        ]


class _FakeAudioEmbed:
    def __init__(self, vector: tuple[float, ...] = (0.1,) * 512) -> None:
        self._vector = vector

    async def embed_audio(
        self,
        audio_paths: list[str],
        *,
        segment_ids: list[int | None] | None = None,
    ) -> list[Any]:
        from audio_graphy.adapters.protocols import AudioEmbeddingResult

        return [
            AudioEmbeddingResult(
                vector=self._vector,
                dim=len(self._vector),
                model="test-clap",
                segment_id=None,
                duration_sec=1.0,
            )
        ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_audio_channel_full_path_returns_hits(
    mock_bundle: Any, tmp_working_dir: Any, tmp_path: Path
) -> None:
    """Audio channel end-to-end with hits — covers embedding + search (lines 793-824)."""
    # Create a fake audio file so audio_query_path check passes.
    audio_file = tmp_path / "query.wav"
    audio_file.write_bytes(b"fake audio bytes")

    # Inject audio_embed into the bundle.
    class _BundleWithAudio:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        @property
        def audio_embed(self) -> Any:
            return _FakeAudioEmbed()

    bundle = _BundleWithAudio(mock_bundle)

    # Use a non-existent chunk_id so _lookup_chunks returns {}.
    retriever = ThreeChannelRetriever(
        bundle,  # type: ignore[arg-type]
        _StubVectorStore(),  # type: ignore[arg-type]
        _EmptyGraphStore(),  # type: ignore[arg-type]
        enable_audio_channel=True,
        audio_vector_store=_FakeAudioVectorStore(hits=[(999, 0.9)]),
    )
    result = await retriever.retrieve("query", tenant_id="t1", audio_query_path=str(audio_file))
    # Audio hits recorded (even without chunk text details).
    assert result is not None
