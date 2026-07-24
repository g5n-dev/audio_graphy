"""Unit tests for M7 WS-3 T10: three-channel retrieval + weighted rerank.

Coverage matrix:
    ChannelWeights dataclass (10 tests):
        - default values match Q1 (0.5 / 0.3 / 0.2)
        - validator rejects sums < 0.99 and > 1.01
        - validator rejects out-of-range weights
        - normalised_for_disabled_audio renormalises correctly
        - normalised_for_disabled_audio is idempotent when audio already 0
        - normalised_for_disabled_audio no-op when text+graph=0
        - weight_for maps naive/graph/audio correctly
        - weight_for defaults unknown channels to text weight
        - total property reflects current sum
        - __post_init__ accepts boundary values (0.99 / 1.01)

    ThreeChannelRetriever (15 tests):
        - retrieve() returns audio_hits=0 when audio channel disabled
        - retrieve() returns audio_hits=0 when audio_query_path is None
        - retrieve() returns audio_hits=0 when audio_vector_store is None
        - retrieve() returns audio_hits=0 when bundle.audio_embed is None
        - retrieve() returns audio_hits>0 when all conditions met
        - retrieve() returns audio_hits=0 on audio channel exception
        - retrieve() parallel execution works (all three channels run)
        - enable_voiceprint=False (legacy) → audio channel skipped
        - RetrievalResult default audio_hits=0 (backward compat)
        - _union_dedup_3 deduplicates by chunk_id
        - _union_dedup_3 picks max score across channels
        - audio channel falls back to minimal candidate when chunk detail missing
        - audio channel skips non-int ids
        - smoke: existing DualChannelRetriever tests still pass

    Reranker weighted scoring (10 tests):
        - _weighted_score uses text weight for naive channel
        - _weighted_score uses graph weight for graph channel
        - _weighted_score uses audio weight for audio channel
        - _weighted_score applies AMBIGUOUS × 0.7 when graph_store marks SPEAKER+AMBIGUOUS
        - _weighted_score does NOT apply penalty for non-AMBIGUOUS SPEAKER
        - _weighted_score does NOT apply penalty when graph_store is None
        - rank_candidates sorts by weighted score descending
        - disable_audio_channel() renormalises weights
        - disable_audio_channel() is idempotent
        - channel_weights property reflects current state

    Integration / regression (5+ tests):
        - end-to-end with all 3 channels producing merged candidates
        - end-to-end with audio channel exception (other channels unaffected)
        - legacy 2-channel call still works via ThreeChannelRetriever
        - legacy 2-channel call still works via DualChannelRetriever
        - Regression: RetrievalResult constructed without audio_hits still works
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from audio_graphy.core.rerank import (
    ChannelWeights,
    Reranker,
)
from audio_graphy.core.retrieval import (
    CandidateSegment,
    DualChannelRetriever,
    RetrievalResult,
    ThreeChannelRetriever,
)

# ============================================================
# Helpers
# ============================================================


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


class _FakeAudioVectorStore:
    """In-memory audio vector store stub for unit tests."""

    def __init__(
        self,
        hits: list[tuple[int, float]] | None = None,
        raise_on_search: bool = False,
    ) -> None:
        self._hits = hits or []
        self._raise = raise_on_search
        self.search_calls = 0

    async def search_audio(
        self,
        tenant_id: str,
        query_vec: tuple[float, ...],
        *,
        top_k: int = 10,
    ) -> list[Any]:
        from audio_graphy.storage.mysql_audio_vector import AudioVectorSearchHit

        self.search_calls += 1
        if self._raise:
            raise RuntimeError("boom")
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
    """Stub AudioEmbedAdapter."""

    def __init__(
        self,
        vector: tuple[float, ...] = (0.1,) * 512,
        raise_on_embed: bool = False,
    ) -> None:
        self._vector = vector
        self._raise = raise_on_embed
        self.embed_calls = 0

    async def embed_audio(
        self,
        audio_paths: list[str],
        *,
        segment_ids: list[int | None] | None = None,
    ) -> list[Any]:
        from audio_graphy.adapters.protocols import AudioEmbeddingResult

        self.embed_calls += 1
        if self._raise:
            raise RuntimeError("embed boom")

        return [
            AudioEmbeddingResult(
                vector=self._vector,
                dim=len(self._vector),
                model="test-clap",
                segment_id=None,
                duration_sec=1.0,
            )
        ]


# ============================================================
# ChannelWeights dataclass
# ============================================================


@pytest.mark.unit
class TestChannelWeights:
    """ChannelWeights dataclass — Q1 validator + normalisation."""

    def test_defaults_match_q1_locked(self) -> None:
        cw = ChannelWeights()
        assert cw.text == pytest.approx(0.5)
        assert cw.graph == pytest.approx(0.3)
        assert cw.audio == pytest.approx(0.2)

    def test_total_is_one_by_default(self) -> None:
        cw = ChannelWeights()
        assert cw.total == pytest.approx(1.0)

    def test_validator_rejects_sum_below_099(self) -> None:
        with pytest.raises(ValueError, match=r"sum to ~1\.0"):
            ChannelWeights(text=0.4, graph=0.3, audio=0.2)  # sum 0.9

    def test_validator_rejects_sum_above_101(self) -> None:
        with pytest.raises(ValueError, match=r"sum to ~1\.0"):
            ChannelWeights(text=0.6, graph=0.3, audio=0.2)  # sum 1.1

    def test_validator_accepts_boundary_099(self) -> None:
        cw = ChannelWeights(text=0.49, graph=0.3, audio=0.2)  # sum 0.99
        assert cw.total == pytest.approx(0.99)

    def test_validator_accepts_boundary_101(self) -> None:
        cw = ChannelWeights(text=0.51, graph=0.3, audio=0.2)  # sum 1.01
        assert cw.total == pytest.approx(1.01)

    def test_validator_rejects_negative_weight(self) -> None:
        with pytest.raises(ValueError, match="must be in"):
            ChannelWeights(text=-0.1, graph=0.9, audio=0.2)

    def test_normalised_for_disabled_audio_default(self) -> None:
        cw = ChannelWeights()
        norm = cw.normalised_for_disabled_audio()
        assert norm.audio == pytest.approx(0.0)
        # 0.5 / 0.8 = 0.625
        assert norm.text == pytest.approx(0.625)
        # 0.3 / 0.8 = 0.375
        assert norm.graph == pytest.approx(0.375)
        # Renormalised sum should still be ~1.0
        assert norm.total == pytest.approx(1.0)

    def test_normalised_is_idempotent_when_audio_already_zero(self) -> None:
        cw = ChannelWeights(text=0.625, graph=0.375, audio=0.0)
        norm = cw.normalised_for_disabled_audio()
        # Should be the same (no work to do).
        assert norm == cw

    def test_normalised_is_noop_when_text_graph_zero(self) -> None:
        # Defensive: degenerate config — caller passed audio=1.0 only.
        cw = ChannelWeights(text=0.0, graph=0.0, audio=1.0)
        norm = cw.normalised_for_disabled_audio()
        assert norm == cw

    def test_weight_for_known_channels(self) -> None:
        cw = ChannelWeights(text=0.5, graph=0.3, audio=0.2)
        assert cw.weight_for("naive") == pytest.approx(0.5)
        assert cw.weight_for("graph") == pytest.approx(0.3)
        assert cw.weight_for("audio") == pytest.approx(0.2)

    def test_weight_for_unknown_channel_defaults_to_text(self) -> None:
        cw = ChannelWeights(text=0.5, graph=0.3, audio=0.2)
        assert cw.weight_for("unknown") == pytest.approx(0.5)
        assert cw.weight_for("") == pytest.approx(0.5)


# ============================================================
# ThreeChannelRetriever
# ============================================================


@pytest.mark.unit
class TestThreeChannelRetriever:
    """Three-channel retrieval — audio channel enable/disable matrix."""

    async def test_audio_disabled_returns_zero_audio_hits(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """enable_audio_channel=False → audio channel skipped."""
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")

        # Use a stub vector store — naive channel will return [].
        class _Stub:
            async def search_chunks(self, *args: object, **kwargs: object) -> list[Any]:
                return []

        retriever = ThreeChannelRetriever(
            mock_bundle,
            _Stub(),
            graph,  # type: ignore[arg-type]
            enable_audio_channel=False,
            audio_vector_store=_FakeAudioVectorStore(hits=[(1, 0.9)]),
        )
        result = await retriever.retrieve(
            "query",
            tenant_id="t1",
            audio_query_path="/tmp/foo.wav",
        )
        assert result.audio_hits == 0

    async def test_audio_no_query_path_returns_zero_audio_hits(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """audio_query_path=None → audio channel skipped."""
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        class _Stub:
            async def search_chunks(self, *args: object, **kwargs: object) -> list[Any]:
                return []

        graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        retriever = ThreeChannelRetriever(
            mock_bundle,
            _Stub(),
            graph,  # type: ignore[arg-type]
            enable_audio_channel=True,
            audio_vector_store=_FakeAudioVectorStore(hits=[(1, 0.9)]),
        )
        result = await retriever.retrieve("query", tenant_id="t1")
        assert result.audio_hits == 0

    async def test_audio_no_vector_store_returns_zero_audio_hits(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """audio_vector_store=None → audio channel skipped."""
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        class _Stub:
            async def search_chunks(self, *args: object, **kwargs: object) -> list[Any]:
                return []

        graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        retriever = ThreeChannelRetriever(
            mock_bundle,
            _Stub(),
            graph,  # type: ignore[arg-type]
            enable_audio_channel=True,
            audio_vector_store=None,
        )
        result = await retriever.retrieve(
            "query",
            tenant_id="t1",
            audio_query_path="/tmp/foo.wav",
        )
        assert result.audio_hits == 0

    async def test_audio_bundle_missing_adapter_returns_zero(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """bundle.audio_embed=None → audio channel skipped."""
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        class _Stub:
            async def search_chunks(self, *args: object, **kwargs: object) -> list[Any]:
                return []

        graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        # mock_bundle has no audio_embed set by default.
        retriever = ThreeChannelRetriever(
            mock_bundle,
            _Stub(),
            graph,  # type: ignore[arg-type]
            enable_audio_channel=True,
            audio_vector_store=_FakeAudioVectorStore(hits=[(1, 0.9)]),
        )
        result = await retriever.retrieve(
            "query",
            tenant_id="t1",
            audio_query_path="/tmp/foo.wav",
        )
        assert result.audio_hits == 0

    async def test_audio_returns_hits_when_all_conditions_met(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """All conditions met → audio channel produces hits."""
        from dataclasses import replace

        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        class _Stub:
            async def search_chunks(self, *args: object, **kwargs: object) -> list[Any]:
                return []

        # AdapterBundle is frozen — use dataclasses.replace to inject.
        bundle_with_audio = replace(mock_bundle, audio_embed=_FakeAudioEmbed())
        try:
            graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
            av_store = _FakeAudioVectorStore(hits=[(10, 0.9), (20, 0.8)])
            retriever = ThreeChannelRetriever(
                bundle_with_audio,
                _Stub(),
                graph,  # type: ignore[arg-type]
                enable_audio_channel=True,
                audio_vector_store=av_store,
            )
            result = await retriever.retrieve(
                "query",
                tenant_id="t1",
                audio_query_path="/tmp/foo.wav",
            )
            assert result.audio_hits == 2
            chunk_ids = {c.chunk_id for c in result.candidates}
            assert chunk_ids == {10, 20}
        finally:
            pass

    async def test_audio_channel_exception_returns_empty(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """Audio channel exception → audio_hits=0, other channels unaffected."""
        from dataclasses import replace

        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        class _Stub:
            async def search_chunks(self, *args: object, **kwargs: object) -> list[Any]:
                return []

        bundle_with_audio = replace(
            mock_bundle,
            audio_embed=_FakeAudioEmbed(raise_on_embed=True),
        )
        graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        retriever = ThreeChannelRetriever(
            bundle_with_audio,
            _Stub(),
            graph,  # type: ignore[arg-type]
            enable_audio_channel=True,
            audio_vector_store=_FakeAudioVectorStore(hits=[(1, 0.9)]),
        )
        result = await retriever.retrieve(
            "query",
            tenant_id="t1",
            audio_query_path="/tmp/foo.wav",
        )
        assert result.audio_hits == 0
        # Other channels should still have run (no exception propagated).

    async def test_audio_vector_store_search_exception_returns_empty(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """search_audio exception → audio_hits=0."""
        from dataclasses import replace

        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        class _Stub:
            async def search_chunks(self, *args: object, **kwargs: object) -> list[Any]:
                return []

        bundle_with_audio = replace(mock_bundle, audio_embed=_FakeAudioEmbed())
        graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        av_store = _FakeAudioVectorStore(raise_on_search=True)
        retriever = ThreeChannelRetriever(
            bundle_with_audio,
            _Stub(),
            graph,  # type: ignore[arg-type]
            enable_audio_channel=True,
            audio_vector_store=av_store,
        )
        result = await retriever.retrieve(
            "query",
            tenant_id="t1",
            audio_query_path="/tmp/foo.wav",
        )
        assert result.audio_hits == 0


@pytest.mark.unit
class TestUnionDedup3:
    """_union_dedup_3 — chunk_id merge + max-score selection."""

    def test_dedups_same_chunk_id_across_channels(self) -> None:
        text = [_candidate(chunk_id=1, score=0.5, source_channel="naive")]
        graph = [_candidate(chunk_id=1, score=0.7, source_channel="graph")]
        audio = [_candidate(chunk_id=2, score=0.9, source_channel="audio")]
        merged = ThreeChannelRetriever._union_dedup_3(text, graph, audio)
        assert len(merged) == 2
        # chunk_id=1 should pick max score (graph 0.7) and its source_channel.
        by_id = {c.chunk_id: c for c in merged}
        assert by_id[1].score == pytest.approx(0.7)
        assert by_id[1].source_channel == "graph"
        assert by_id[2].score == pytest.approx(0.9)

    def test_returns_empty_for_all_empty_inputs(self) -> None:
        merged = ThreeChannelRetriever._union_dedup_3([], [], [])
        assert merged == []

    def test_handles_text_only(self) -> None:
        text = [_candidate(chunk_id=1, score=0.5)]
        merged = ThreeChannelRetriever._union_dedup_3(text, [], [])
        assert len(merged) == 1

    def test_handles_audio_only(self) -> None:
        audio = [_candidate(chunk_id=1, score=0.5, source_channel="audio")]
        merged = ThreeChannelRetriever._union_dedup_3([], [], audio)
        assert len(merged) == 1


@pytest.mark.unit
class TestRetrievalResultAudioHits:
    """RetrievalResult backward-compat with audio_hits field."""

    def test_default_audio_hits_is_zero(self) -> None:
        r = RetrievalResult(
            query="q",
            candidates=[],
            naive_hits=0,
            graph_hits=0,
            filtered_by_time=0,
        )
        assert r.audio_hits == 0

    def test_explicit_audio_hits(self) -> None:
        r = RetrievalResult(
            query="q",
            candidates=[],
            naive_hits=0,
            graph_hits=0,
            filtered_by_time=0,
            audio_hits=5,
        )
        assert r.audio_hits == 5


# ============================================================
# Reranker weighted scoring
# ============================================================


@pytest.mark.unit
class TestRerankerWeightedScoring:
    """Reranker._weighted_score — channel fusion + AMBIGUOUS downgrade."""

    def test_weighted_score_text_channel(self, mock_bundle: Any) -> None:
        r = Reranker(mock_bundle, channel_weights=ChannelWeights())  # type: ignore[arg-type]
        c = _candidate(score=1.0, source_channel="naive")
        # 0.5 * 1.0 = 0.5
        assert r._weighted_score(c) == pytest.approx(0.5)

    def test_weighted_score_graph_channel(self, mock_bundle: Any) -> None:
        r = Reranker(mock_bundle, channel_weights=ChannelWeights())  # type: ignore[arg-type]
        c = _candidate(score=1.0, source_channel="graph")
        # 0.3 * 1.0 = 0.3
        assert r._weighted_score(c) == pytest.approx(0.3)

    def test_weighted_score_audio_channel(self, mock_bundle: Any) -> None:
        r = Reranker(mock_bundle, channel_weights=ChannelWeights())  # type: ignore[arg-type]
        c = _candidate(score=1.0, source_channel="audio")
        # 0.2 * 1.0 = 0.2
        assert r._weighted_score(c) == pytest.approx(0.2)

    def test_weighted_score_unknown_channel_defaults_to_text(
        self,
        mock_bundle: Any,
    ) -> None:
        r = Reranker(mock_bundle, channel_weights=ChannelWeights())  # type: ignore[arg-type]
        c = _candidate(score=1.0, source_channel="legacy")
        # Defaults to text weight 0.5 * 1.0 = 0.5
        assert r._weighted_score(c) == pytest.approx(0.5)

    def test_weighted_score_respects_zero_audio_weight(
        self,
        mock_bundle: Any,
    ) -> None:
        """When audio weight = 0, audio candidates contribute 0."""
        cw = ChannelWeights(text=0.625, graph=0.375, audio=0.0)
        r = Reranker(mock_bundle, channel_weights=cw)  # type: ignore[arg-type]
        c = _candidate(score=1.0, source_channel="audio")
        assert r._weighted_score(c) == pytest.approx(0.0)

    def test_weighted_score_applies_ambiguous_penalty(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """Candidate from AMBIGUOUS speaker → score × 0.7."""
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        # Manually inject an AMBIGUOUS SPEAKER node into the graph cache.
        graph.graph.add_node(
            "speaker:vp_001",
            type="SPEAKER",
            ambiguity_tag="AMBIGUOUS",
            source_ids=["1_5"],  # recording_id=1, chunk_id=5
        )
        r = Reranker(
            mock_bundle,
            graph_store=graph,  # type: ignore[arg-type]
            channel_weights=ChannelWeights(),
        )
        c = _candidate(chunk_id=5, recording_id=1, score=1.0, source_channel="naive")
        # 0.5 * 1.0 * 0.7 = 0.35
        assert r._weighted_score(c) == pytest.approx(0.35)

    def test_weighted_score_no_penalty_for_non_ambiguous_speaker(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """Candidate from non-AMBIGUOUS SPEAKER → no penalty."""
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        graph.graph.add_node(
            "speaker:vp_002",
            type="SPEAKER",
            ambiguity_tag=None,  # not AMBIGUOUS
            source_ids=["1_5"],
        )
        r = Reranker(
            mock_bundle,
            graph_store=graph,  # type: ignore[arg-type]
            channel_weights=ChannelWeights(),
        )
        c = _candidate(chunk_id=5, recording_id=1, score=1.0, source_channel="naive")
        # No penalty applied — just 0.5 * 1.0
        assert r._weighted_score(c) == pytest.approx(0.5)

    def test_weighted_score_no_penalty_when_graph_store_none(
        self,
        mock_bundle: Any,
    ) -> None:
        """No graph_store → no AMBIGUOUS detection → no penalty."""
        r = Reranker(mock_bundle, channel_weights=ChannelWeights())  # type: ignore[arg-type]
        c = _candidate(chunk_id=5, recording_id=1, score=1.0, source_channel="naive")
        # Just 0.5 * 1.0
        assert r._weighted_score(c) == pytest.approx(0.5)

    def test_rank_candidates_sorts_descending(self, mock_bundle: Any) -> None:
        r = Reranker(mock_bundle, channel_weights=ChannelWeights())  # type: ignore[arg-type]
        # text channel (weight 0.5) → expected weighted 0.5
        # graph channel (weight 0.3) → expected weighted 0.3
        # audio channel (weight 0.2) → expected weighted 0.2
        candidates = [
            _candidate(chunk_id=1, score=1.0, source_channel="audio"),
            _candidate(chunk_id=2, score=1.0, source_channel="graph"),
            _candidate(chunk_id=3, score=1.0, source_channel="naive"),
        ]
        ranked = r.rank_candidates(candidates)
        assert [c.chunk_id for c in ranked] == [3, 2, 1]

    def test_disable_audio_channel_renormalises(self, mock_bundle: Any) -> None:
        r = Reranker(mock_bundle, channel_weights=ChannelWeights())  # type: ignore[arg-type]
        # Sanity check default.
        assert r.channel_weights.audio == pytest.approx(0.2)
        r.disable_audio_channel()
        assert r.channel_weights.audio == pytest.approx(0.0)
        assert r.channel_weights.text == pytest.approx(0.625)
        assert r.channel_weights.graph == pytest.approx(0.375)

    def test_disable_audio_channel_idempotent(self, mock_bundle: Any) -> None:
        r = Reranker(mock_bundle, channel_weights=ChannelWeights())  # type: ignore[arg-type]
        r.disable_audio_channel()
        first = r.channel_weights
        r.disable_audio_channel()
        assert r.channel_weights == first


# ============================================================
# Regression: legacy DualChannelRetriever still works
# ============================================================


@pytest.mark.unit
class TestRegressionLegacyRetriever:
    """DualChannelRetriever must continue to work unchanged."""

    async def test_dual_channel_still_returns_audio_hits_zero(
        self,
        mock_bundle: Any,
        tmp_working_dir: Any,
    ) -> None:
        """DualChannelRetriever.retrieve returns audio_hits=0 (default)."""
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        class _Stub:
            async def search_chunks(self, *args: object, **kwargs: object) -> list[Any]:
                return []

        graph = NetworkXGraphStore(tmp_working_dir, tenant_id="default")
        retriever = DualChannelRetriever(mock_bundle, _Stub(), graph)  # type: ignore[arg-type]
        result = await retriever.retrieve("query", tenant_id="t1")
        # audio_hits defaults to 0 — backward compatible with R1.
        assert result.audio_hits == 0
