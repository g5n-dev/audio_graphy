"""Reranker — LLM as-judge filtering + refined reranking + answer generation.

Four-stage pipeline (DESIGN.md §3.3 stages 3-4):
    1. LLM as-judge: strong_llm evaluates each candidate → yes/no (filter)
    2. Keyword extraction: weak_llm extracts query keywords (cached)
    3. Refined reranking: ASR re-transcription (mock=original) + description upgrade
    4. Answer generation: strong_llm generates final answer + 3-level provenance citations

Error handling (PRD §4.5):
    - LLM judge failure → keep candidate (conservative: prefer false positives)
    - Keyword extraction failure → use original query
    - ASR re-transcription failure → use original transcript
    - Answer generation failure → answer="（生成失败）", citations still returned
    - Empty candidates → answer="未找到相关录音片段"
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.protocols import EdgeConfidence, LLMResponse
from audio_graphy.core.retrieval import CandidateSegment

if TYPE_CHECKING:
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

logger = logging.getLogger(__name__)


# ============================================================
# Data classes
# ============================================================


@dataclass(frozen=True, slots=True)
class Citation:
    """A citation in the final answer — 3-level provenance chain.

    Attributes:
        entity: Matched entity name.
        chunk_id: Provenance 1: entity → chunk.
        segment_ids: Provenance 2: chunk → segments.
        recording_id: Provenance 3: segment → recording.
        recorded_at: Recording timestamp.
        transcript_snippet: Segment-level transcript excerpt (refined).
        confidence: Associated edge confidence (EXTRACTED/INFERRED/AMBIGUOUS).
    """

    entity: str
    chunk_id: int
    segment_ids: list[int]
    recording_id: int
    recorded_at: datetime | None
    transcript_snippet: str
    confidence: EdgeConfidence


@dataclass(frozen=True, slots=True)
class RerankResult:
    """Complete rerank + answer generation output.

    Attributes:
        answer: Final answer text.
        citations: 3-level provenance citation list.
        filtered_count: Number of candidates removed by LLM as-judge.
        refined_count: Number of candidates that went through refinement.
    """

    answer: str
    citations: list[Citation]
    filtered_count: int
    refined_count: int


# ============================================================
# Reranker
# ============================================================


class Reranker:
    """LLM as-judge filtering + refined reranking + answer generation.

    Args:
        bundle: AdapterBundle (strong_llm for judge/answer, weak_llm for keywords).
        file_index: Optional FileIndex for LLM response cache (Layer 2).
        graph_store: Optional NetworkXGraphStore for entity/confidence lookup.
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        *,
        file_index: FileIndex | None = None,
        graph_store: NetworkXGraphStore | None = None,
    ) -> None:
        self._bundle = bundle
        self._file_index = file_index
        self._graph_store = graph_store

    async def rerank_and_answer(
        self,
        query: str,
        candidates: Sequence[CandidateSegment],
        *,
        time_range: tuple[datetime, datetime] | None = None,
    ) -> RerankResult:
        """Execute the full rerank + answer pipeline.

        Args:
            query: User query.
            candidates: Retrieval candidates.
            time_range: Optional time range (for context in answer generation).

        Returns:
            RerankResult with answer, citations, and counts.
        """
        # Handle empty candidates
        if not candidates:
            return RerankResult(
                answer="未找到相关录音片段",
                citations=[],
                filtered_count=0,
                refined_count=0,
            )

        # Step 1: LLM as-judge filter
        surviving, filtered_count = await self._llm_judge_filter(query, candidates)

        # Step 2: Keyword extraction
        keywords = await self._extract_keywords(query)

        # Step 3: Refined reranking
        refined = await self._refine_descriptions(surviving, keywords)

        # Step 4: Build citations
        citations = await self._build_citations(refined)

        # Step 5: Answer generation
        answer = await self._generate_answer(query, refined, citations, time_range)

        return RerankResult(
            answer=answer,
            citations=citations,
            filtered_count=filtered_count,
            refined_count=len(refined),
        )

    # ------------------------------------------------------------------
    # LLM as-judge filter
    # ------------------------------------------------------------------

    async def _llm_judge_filter(
        self,
        query: str,
        candidates: Sequence[CandidateSegment],
    ) -> tuple[list[CandidateSegment], int]:
        """Filter candidates by LLM as-judge (yes/no relevance).

        Conservative strategy: if LLM judge fails, KEEP the candidate
        (prefer false positives over false negatives).

        Args:
            query: User query.
            candidates: All retrieval candidates.

        Returns:
            Tuple of (surviving_candidates, filtered_count).
        """
        surviving: list[CandidateSegment] = []
        filtered_count = 0

        for cand in candidates:
            try:
                prompt = (
                    f"请判断以下录音段是否与用户问题相关。\n"
                    f"问题: {query}\n"
                    f"段文本: {cand.text[:500]}\n"
                    f"请回答 yes 或 no。"
                )
                messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
                response = await self._cached_complete_strong(messages)

                # Parse yes/no
                text_lower = response.text.lower().strip()
                if "no" in text_lower and "yes" not in text_lower:
                    filtered_count += 1
                    continue
                # "yes" or ambiguous → keep
                surviving.append(cand)

            except Exception as exc:
                logger.warning(
                    "LLM judge failed for chunk %d, keeping candidate: %s", cand.chunk_id, exc
                )
                surviving.append(cand)  # Conservative: keep on failure

        return surviving, filtered_count

    # ------------------------------------------------------------------
    # Keyword extraction
    # ------------------------------------------------------------------

    async def _extract_keywords(self, query: str) -> list[str]:
        """Extract keywords from query via weak_llm.

        Falls back to simple split if LLM fails.

        Args:
            query: User query.

        Returns:
            List of keyword strings.
        """
        import re

        try:
            messages: list[dict[str, str]] = [
                {
                    "role": "user",
                    "content": f"请从以下问题中提取关键词，用逗号分隔返回：\n{query}",
                }
            ]
            response = await self._cached_complete_weak(messages)

            text = re.sub(r"^(关键词|keywords?)[:：]?\s*", "", response.text, flags=re.IGNORECASE)
            parts = re.split(r"[,，;；\n、]+", text)
            keywords = [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]
            if keywords:
                return keywords
        except Exception as exc:
            logger.warning("Keyword extraction failed, using fallback: %s", exc)

        # Fallback: simple split
        parts = re.split(r"[，。？！,.!?;\s\n、（）()]+", query)
        return [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]

    # ------------------------------------------------------------------
    # Refined reranking
    # ------------------------------------------------------------------

    async def _refine_descriptions(
        self,
        candidates: list[CandidateSegment],
        keywords: list[str],
    ) -> list[CandidateSegment]:
        """Refine candidate descriptions via ASR re-transcription + keyword highlight.

        In M2 mock mode, ASR re-transcription returns the original transcript
        (Q5 decision). The "refined description" is the original text with
        keywords highlighted (for answer generation context).

        Args:
            candidates: Surviving candidates after LLM judge.
            keywords: Query keywords for refinement.

        Returns:
            Refined candidate segments (text unchanged in mock, but interface ready).
        """
        refined: list[CandidateSegment] = []
        for cand in candidates:
            # M2: ASR re-transcription returns original transcript
            # Phase 2: call bundle.asr.transcribe() for high-precision re-transcription
            refined_text = cand.text  # Mock: no change

            # Create refined candidate (text unchanged, but could be upgraded)
            refined.append(
                CandidateSegment(
                    chunk_id=cand.chunk_id,
                    recording_id=cand.recording_id,
                    segment_ids=cand.segment_ids,
                    text=refined_text,
                    recorded_at=cand.recorded_at,
                    score=cand.score,
                    source_channel=cand.source_channel,
                )
            )
        return refined

    # ------------------------------------------------------------------
    # Citation building
    # ------------------------------------------------------------------

    async def _build_citations(self, candidates: list[CandidateSegment]) -> list[Citation]:
        """Build 3-level provenance citations from refined candidates.

        Args:
            candidates: Refined candidate segments.

        Returns:
            List of Citation objects with full provenance chain.
        """
        citations: list[Citation] = []

        for cand in candidates:
            # Look up entity name from graph_store (if available)
            entity_name = "未知实体"
            confidence: EdgeConfidence = "EXTRACTED"

            if self._graph_store is not None:
                entity_name, confidence = await self._lookup_entity_for_chunk(
                    cand.chunk_id, cand.recording_id
                )

            # Transcript snippet: first 200 chars of text
            snippet = cand.text[:200] if cand.text else ""

            citations.append(
                Citation(
                    entity=entity_name,
                    chunk_id=cand.chunk_id,
                    segment_ids=list(cand.segment_ids),
                    recording_id=cand.recording_id,
                    recorded_at=cand.recorded_at,
                    transcript_snippet=snippet,
                    confidence=confidence,
                )
            )

        return citations

    async def _lookup_entity_for_chunk(
        self, chunk_id: int, recording_id: int
    ) -> tuple[str, EdgeConfidence]:
        """Look up entity name and edge confidence for a chunk.

        Searches the graph for nodes whose source_ids reference this chunk.

        Args:
            chunk_id: Chunk database ID.
            recording_id: Recording ID.

        Returns:
            Tuple of (entity_name, confidence).
        """
        if self._graph_store is None:
            return "未知实体", "EXTRACTED"

        source_id = f"{recording_id}_{chunk_id}"
        try:
            all_nodes = await self._graph_store.get_all_nodes()
            for node in all_nodes:
                if source_id in node.source_ids:
                    # Found entity — look up edge confidence
                    edges = await self._graph_store.get_edges(node.entity_id)
                    if edges:
                        # Use the highest-confidence edge
                        best_edge = max(edges, key=lambda e: _confidence_rank(e.confidence))
                        return node.name, best_edge.confidence
                    return node.name, "EXTRACTED"
        except Exception as exc:
            logger.warning("Entity lookup failed for chunk %d: %s", chunk_id, exc)

        return "未知实体", "EXTRACTED"

    # ------------------------------------------------------------------
    # Answer generation
    # ------------------------------------------------------------------

    async def _generate_answer(
        self,
        query: str,
        candidates: list[CandidateSegment],
        citations: list[Citation],
        time_range: tuple[datetime, datetime] | None,
    ) -> str:
        """Generate final answer via strong_llm.

        Args:
            query: User query.
            candidates: Refined candidate segments.
            citations: Provenance citations.
            time_range: Optional time range context.

        Returns:
            Answer text (or "（生成失败）" on failure).
        """
        # Build context from candidates
        context_parts: list[str] = []
        for i, (cand, cite) in enumerate(zip(candidates, citations, strict=True), 1):
            time_str = cite.recorded_at.strftime("%Y-%m-%d") if cite.recorded_at else "未知时间"
            context_parts.append(
                f"[{i}] 录音{cite.recording_id} ({time_str}) "
                f"实体:{cite.entity} 段:{cite.segment_ids}\n"
                f"内容: {cand.text[:300]}"
            )
        context = "\n\n".join(context_parts)

        time_context = ""
        if time_range is not None:
            time_context = f"时间范围: {time_range[0].strftime('%Y-%m-%d')} 至 {time_range[1].strftime('%Y-%m-%d')}\n"

        prompt = (
            f"请根据以下录音段信息回答用户问题。\n"
            f"{time_context}"
            f"问题: {query}\n\n"
            f"相关录音段:\n{context}\n\n"
            f"请生成回答，并在引用处标注序号 [1] [2] 等。"
        )

        try:
            messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
            response = await self._cached_complete_strong(messages)
            return response.text
        except Exception as exc:
            logger.warning("Answer generation failed: %s", exc)
            return "（生成失败）"

    # ------------------------------------------------------------------
    # LLM cache helpers (dual-layer)
    # ------------------------------------------------------------------

    async def _cached_complete_strong(self, messages: list[dict[str, str]]) -> LLMResponse:
        """Call strong_llm with dual-layer cache."""
        return await self._cached_complete(self._bundle.strong_llm, messages)

    async def _cached_complete_weak(self, messages: list[dict[str, str]]) -> LLMResponse:
        """Call weak_llm with dual-layer cache."""
        return await self._cached_complete(self._bundle.weak_llm, messages)

    async def _cached_complete(
        self,
        adapter: object,
        messages: list[dict[str, str]],
    ) -> LLMResponse:
        """Call LLM adapter with dual-layer cache (file_index + adapter).

        Args:
            adapter: LLM adapter (strong or weak).
            messages: Chat messages.

        Returns:
            LLMResponse (cached=True if cache hit).
        """
        model = adapter.model  # type: ignore[attr-defined]
        cache_key = self._compute_cache_key(model, messages)

        # Layer 2: Check file_index persistent cache
        if self._file_index is not None:
            cached_text = await self._file_index.get_llm_cache(cache_key)
            if cached_text is not None:
                return LLMResponse(
                    text=cached_text,
                    model=model,
                    prompt_hash=cache_key,
                    cached=True,
                    usage={},
                )

        # Layer 1 + API: Call adapter
        response = await adapter.complete(  # type: ignore[attr-defined]
            messages=messages,
            cache_key=cache_key,
        )

        # Store in Layer 2
        if self._file_index is not None and not response.cached:
            await self._file_index.set_llm_cache(cache_key, response.text)

        return response  # type: ignore[no-any-return]

    @staticmethod
    def _compute_cache_key(model: str, messages: Sequence[dict[str, str]]) -> str:
        """Compute LLM cache key = MD5(model, messages)."""
        payload = json.dumps(
            {"model": model, "messages": list(messages)},
            ensure_ascii=False,
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()


# ============================================================
# Confidence ranking helper
# ============================================================

_CONFIDENCE_RANK: dict[str, int] = {
    "AMBIGUOUS": 0,
    "INFERRED": 1,
    "EXTRACTED": 2,
}


def _confidence_rank(confidence: str) -> int:
    """Return numeric rank for confidence (higher = better)."""
    return _CONFIDENCE_RANK.get(confidence, 0)
