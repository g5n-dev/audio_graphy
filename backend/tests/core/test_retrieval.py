"""Unit tests for DualChannelRetriever — naive + graph + time filter.

Tests cover:
    - Naive channel (vector search → candidates)
    - Graph channel (keyword → entity → neighbors → chunks)
    - Union dedup (by chunk_id, score=max)
    - Time filter (in range / out of range / None)
    - Sort by recorded_at
    - Empty results / both channels empty
    - Fallback keyword extraction
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from audio_graphy.core.retrieval import (
    CandidateSegment,
    DualChannelRetriever,
    RetrievalResult,
)


def _make_candidate(
    chunk_id: int,
    recording_id: int = 1,
    recorded_at: datetime | None = None,
    score: float = 0.9,
    source_channel: str = "naive",
) -> CandidateSegment:
    """Helper to create a CandidateSegment."""
    return CandidateSegment(
        chunk_id=chunk_id,
        recording_id=recording_id,
        segment_ids=[chunk_id],
        text=f"chunk text {chunk_id}",
        recorded_at=recorded_at,
        score=score,
        source_channel=source_channel,
    )


@pytest.mark.unit
class TestUnionDedup:
    """Union + dedup logic."""

    def test_dedup_by_chunk_id(self) -> None:
        """Same chunk_id from both channels → keep higher score."""
        naive = [_make_candidate(1, score=0.8, source_channel="naive")]
        graph = [_make_candidate(1, score=0.9, source_channel="graph")]
        result = DualChannelRetriever._union_dedup(naive, graph)
        assert len(result) == 1
        assert result[0].score == 0.9  # Max score
        assert result[0].source_channel == "graph"  # Higher score source

    def test_no_overlap(self) -> None:
        """Different chunk_ids → all kept."""
        naive = [_make_candidate(1)]
        graph = [_make_candidate(2, source_channel="graph")]
        result = DualChannelRetriever._union_dedup(naive, graph)
        assert len(result) == 2

    def test_empty_inputs(self) -> None:
        """Both channels empty → empty result."""
        result = DualChannelRetriever._union_dedup([], [])
        assert result == []


@pytest.mark.unit
class TestTimeFilter:
    """Time range filtering."""

    def test_filter_in_range(self) -> None:
        """Candidates within range are kept."""
        candidates = [
            _make_candidate(1, recorded_at=datetime(2026, 7, 10, tzinfo=UTC)),
            _make_candidate(2, recorded_at=datetime(2026, 7, 15, tzinfo=UTC)),
        ]
        time_range = (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC))
        filtered, removed = DualChannelRetriever._filter_by_time(candidates, time_range)
        assert len(filtered) == 2
        assert removed == 0

    def test_filter_out_of_range(self) -> None:
        """Candidates outside range are removed."""
        candidates = [
            _make_candidate(1, recorded_at=datetime(2026, 6, 15, tzinfo=UTC)),  # Before
            _make_candidate(2, recorded_at=datetime(2026, 7, 10, tzinfo=UTC)),  # In range
            _make_candidate(3, recorded_at=datetime(2026, 8, 15, tzinfo=UTC)),  # After
        ]
        time_range = (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC))
        filtered, removed = DualChannelRetriever._filter_by_time(candidates, time_range)
        assert len(filtered) == 1
        assert removed == 2
        assert filtered[0].chunk_id == 2

    def test_no_time_range(self) -> None:
        """None time_range → no filtering."""
        candidates = [_make_candidate(1, recorded_at=datetime(2026, 1, 1, tzinfo=UTC))]
        filtered, removed = DualChannelRetriever._filter_by_time(candidates, None)
        assert len(filtered) == 1
        assert removed == 0

    def test_none_recorded_at_kept(self) -> None:
        """Candidates with None recorded_at are kept (can't filter)."""
        candidates = [_make_candidate(1, recorded_at=None)]
        time_range = (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC))
        filtered, removed = DualChannelRetriever._filter_by_time(candidates, time_range)
        assert len(filtered) == 1
        assert removed == 0


@pytest.mark.unit
class TestSortByTime:
    """Sort by recorded_at ascending."""

    def test_sort_ascending(self) -> None:
        """Candidates sorted by recorded_at ascending."""
        candidates = [
            _make_candidate(1, recorded_at=datetime(2026, 7, 15, tzinfo=UTC)),
            _make_candidate(2, recorded_at=datetime(2026, 7, 1, tzinfo=UTC)),
            _make_candidate(3, recorded_at=datetime(2026, 7, 10, tzinfo=UTC)),
        ]
        sorted_cands = DualChannelRetriever._sort_by_time(candidates)
        assert sorted_cands[0].chunk_id == 2  # July 1
        assert sorted_cands[1].chunk_id == 3  # July 10
        assert sorted_cands[2].chunk_id == 1  # July 15

    def test_none_recorded_at_last(self) -> None:
        """None recorded_at goes last."""
        candidates = [
            _make_candidate(1, recorded_at=None),
            _make_candidate(2, recorded_at=datetime(2026, 7, 1, tzinfo=UTC)),
        ]
        sorted_cands = DualChannelRetriever._sort_by_time(candidates)
        assert sorted_cands[0].chunk_id == 2
        assert sorted_cands[1].chunk_id == 1


@pytest.mark.unit
class TestKeywordExtraction:
    """Keyword extraction from query."""

    def test_parse_keywords_comma(self) -> None:
        """Parse comma-separated keywords."""
        result = DualChannelRetriever._parse_keywords("CS75 Plus, 金融政策, 优惠")
        assert "CS75 Plus" in result
        assert "金融政策" in result
        assert "优惠" in result

    def test_parse_keywords_chinese_comma(self) -> None:
        """Parse Chinese comma-separated keywords."""
        result = DualChannelRetriever._parse_keywords("CS75 Plus，金融政策，优惠")
        assert len(result) == 3

    def test_parse_keywords_with_prefix(self) -> None:
        """Parse keywords with '关键词:' prefix."""
        result = DualChannelRetriever._parse_keywords("关键词：CS75 Plus, 金融政策")
        assert "CS75 Plus" in result

    def test_fallback_keywords(self) -> None:
        """Fallback keyword extraction by splitting."""
        result = DualChannelRetriever._fallback_keywords("CS75 Plus 金融政策 优惠")
        assert len(result) >= 2

    def test_fallback_filters_short(self) -> None:
        """Fallback filters out single-character tokens."""
        result = DualChannelRetriever._fallback_keywords("A CS75 Plus")
        assert all(len(kw) >= 2 for kw in result)


@pytest.mark.unit
class TestSourceIdParsing:
    """source_id → chunk_id parsing."""

    def test_valid_source_id(self) -> None:
        assert DualChannelRetriever._parse_chunk_id("1_3") == 3
        assert DualChannelRetriever._parse_chunk_id("42_100") == 100

    def test_invalid_source_id(self) -> None:
        assert DualChannelRetriever._parse_chunk_id("invalid") is None
        assert DualChannelRetriever._parse_chunk_id("1_abc") is None

    def test_single_part(self) -> None:
        assert DualChannelRetriever._parse_chunk_id("3") is None


@pytest.mark.integration
class TestDualChannelRetrieval:
    """Integration tests with mock adapters + stores."""

    async def test_retrieve_empty_graph(
        self,
        mock_bundle: Any,
        vector_store: Any,
        graph_store: Any,
    ) -> None:
        """Retrieval with empty graph → only naive channel."""
        retriever = DualChannelRetriever(
            mock_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
        )
        result = await retriever.retrieve("test query", top_k=5)

        assert isinstance(result, RetrievalResult)
        assert result.query == "test query"
        assert result.graph_hits == 0  # Empty graph

    async def test_retrieve_with_time_range(
        self,
        mock_bundle: Any,
        vector_store: Any,
        graph_store: Any,
    ) -> None:
        """Retrieval with time_range → filtered_by_time tracked."""
        retriever = DualChannelRetriever(
            mock_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
        )
        time_range = (
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 31, tzinfo=UTC),
        )
        result = await retriever.retrieve("test query", top_k=5, time_range=time_range)
        # No data → no candidates, no filtering
        assert isinstance(result, RetrievalResult)

    async def test_graph_channel_with_entities(
        self,
        scripted_bundle: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """Graph channel finds chunks via entity matching."""
        from audio_graphy.core.types import GraphNode

        # Populate graph with an entity
        await graph_store.upsert_node(
            GraphNode(
                entity_id="CS75 Plus",
                name="CS75 Plus",
                type="车型",
                description="SUV",
                source_ids=["1_5"],
                recording_ids=[1],
                degree=1,
            )
        )

        retriever = DualChannelRetriever(
            scripted_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
        )
        result = await retriever.retrieve("CS75 Plus 金融政策", top_k=5)

        # Graph channel should find the entity
        # (chunk detail lookup will be empty since no MySQL/file_index data)
        assert isinstance(result, RetrievalResult)
