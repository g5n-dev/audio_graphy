"""Reranker — LLM as-judge filtering + refined reranking + answer generation.

Four-stage pipeline (DESIGN.md §3.3 stages 3-4):
    1. LLM as-judge: strong_llm evaluates each candidate → yes/no (filter)
    2. Keyword extraction: weak_llm extracts query keywords (cached)
    3. Refined reranking: ASR re-transcription (mock=original) + description upgrade
    4. Answer generation: strong_llm generates final answer + 3-level provenance citations

M7 additions (architecture §10.2):
    - ``ChannelWeights`` dataclass with validator (Q1 locked: text 0.5 /
      graph 0.3 / audio 0.2). ``enable_voiceprint=False`` callers should
      normalise to ``(0.625, 0.375, 0.0)`` — see ``ChannelWeights.normalise``.
    - ``Reranker.channel_weights`` constructor argument.
    - ``_weighted_score()`` fuses candidates by their source channel.
    - AMBIGUOUS speaker candidate gets ``score × 0.7`` (§10.3) before
      channel weighting — not剔除 (§17.6).
    - When channel_weights.audio == 0.0, audio channel candidates still
      participate in ranking (they just contribute 0 from the weight
      multiplier); the sort is driven by the candidate's pre-weight score.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.protocols import EdgeConfidence, LLMResponse
from audio_graphy.core.language_detection import (
    detect_semantic_language,
    semantic_protected_identifiers,
)
from audio_graphy.core.retrieval import CandidateSegment
from audio_graphy.llm.gateway import (
    CachePolicy,
    LLMProvenance,
    LLMRequest,
    execute_llm,
    lookup_llm_cache,
    store_validated_llm_cache,
)

if TYPE_CHECKING:
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

logger = logging.getLogger(__name__)

_RELEVANCE_PROMPT_VERSION = "relevance-judge-prefix-v2"
_RELEVANCE_SCHEMA_VERSION = "yes-no-relevance-v1"
_RELEVANCE_PARSER_VERSION = "strict-yes-no-v1"
_RELEVANCE_POSTPROCESSOR_VERSION = "conservative-keep-v1"
_BATCH_RELEVANCE_SCHEMA_VERSION = "batch-relevance-verdicts-v1"
_BATCH_RELEVANCE_PARSER_VERSION = "batch-verdict-json-v1"
_KEYWORD_PROMPT_VERSION = "query-keywords-prefix-v2"
_KEYWORD_SCHEMA_VERSION = "comma-separated-keywords-v1"
_KEYWORD_PARSER_VERSION = "keyword-delimiters-v1"
_KEYWORD_POSTPROCESSOR_VERSION = "keyword-min-length-v1"
_FINAL_ANSWER_PROMPT_VERSION = "grounded-answer-citations-prefix-v2"
_FINAL_ANSWER_SCHEMA_VERSION = "answer-with-inline-citations-v1"
_FINAL_ANSWER_PARSER_VERSION = "plain-text-answer-v1"
_FINAL_ANSWER_POSTPROCESSOR_VERSION = "answer-failure-sentinel-v1"
_QUERY_HELPER_TTL_SECONDS = 7 * 24 * 60 * 60
_RELEVANCE_TTL_SECONDS = 7 * 24 * 60 * 60
_FINAL_ANSWER_TTL_SECONDS = 5 * 60


def _resolved_permission_scope(
    tenant_id: str,
    permission_scope: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a non-empty authorization snapshot for recipe isolation."""

    return dict(permission_scope) if permission_scope else {"tenant_id": tenant_id}


def _query_sha256(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _candidate_snapshot(candidate: CandidateSegment) -> dict[str, Any]:
    """Return stable, content-addressed candidate state for an LLM recipe."""

    return {
        "recording_id": candidate.recording_id,
        "chunk_id": candidate.chunk_id,
        "segment_ids": list(candidate.segment_ids),
        "recorded_at": candidate.recorded_at.isoformat() if candidate.recorded_at else None,
        "text_sha256": hashlib.sha256(candidate.text.encode("utf-8")).hexdigest(),
        "source_channel": candidate.source_channel,
    }


def _request_provenance(
    query_sha256: str,
    candidates: Sequence[CandidateSegment] = (),
) -> tuple[LLMProvenance, ...]:
    """Build deduplicated query/recording/chunk references for DSAR invalidation."""

    refs: list[LLMProvenance] = [LLMProvenance("query", query_sha256)]
    seen: set[tuple[str, str]] = {("query", query_sha256)}
    for candidate in candidates:
        for source_type, source_id in (
            ("recording", str(candidate.recording_id)),
            ("chunk", str(candidate.chunk_id)),
        ):
            key = (source_type, source_id)
            if key not in seen:
                seen.add(key)
                refs.append(LLMProvenance(source_type, source_id))
    return tuple(refs)


def _valid_yes_no_response(response: LLMResponse) -> bool:
    """Accept only one unambiguous relevance verdict for cache writes."""

    return _yes_no_verdict(response) is not None


def _yes_no_verdict(response: LLMResponse) -> str | None:
    """Parse one unambiguous relevance verdict, otherwise fail open."""

    text = response.text.casefold().strip()
    return text if text in {"yes", "no"} else None


def _valid_batch_response(
    response: LLMResponse,
    candidate_ids: Sequence[str],
) -> bool:
    """Require one schema-valid verdict per candidate before caching a batch."""

    try:
        payload = json.loads(response.text)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict) or set(payload) != {"verdicts"}:
        return False
    verdicts = payload["verdicts"]
    if not isinstance(verdicts, list) or len(verdicts) != len(candidate_ids):
        return False
    expected = set(candidate_ids)
    seen: set[str] = set()
    for verdict in verdicts:
        if not isinstance(verdict, dict) or set(verdict) != {
            "candidate_id",
            "verdict",
            "reason",
        }:
            return False
        candidate_id = verdict["candidate_id"]
        if not isinstance(candidate_id, str) or candidate_id not in expected:
            return False
        if candidate_id in seen:
            return False
        if verdict["verdict"] not in {"yes", "no"} or not isinstance(
            verdict["reason"],
            str,
        ):
            return False
        seen.add(candidate_id)
    return seen == expected


def _valid_final_answer(response: LLMResponse) -> bool:
    """Do not persist empty or explicit failure-sentinel final answers."""

    text = response.text.strip()
    return bool(text) and text != "（生成失败）"


def _batch_usage_share(
    usage: Mapping[str, int],
    *,
    part_index: int,
    part_count: int,
) -> dict[str, int]:
    """Deterministically allocate batch token usage across ordered misses."""

    if part_count <= 0 or not 0 <= part_index < part_count:
        raise ValueError("batch usage share requires a valid part index/count")
    share: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        total = usage.get(key)
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            continue
        quotient, remainder = divmod(total, part_count)
        share[key] = quotient + int(part_index < remainder)
    return share


# ============================================================
# Channel weights (M7 §10.2 — Q1 locked)
# ============================================================


@dataclass(frozen=True, slots=True)
class ChannelWeights:
    """Rerank channel weights (Q1 locked: 0.5 / 0.3 / 0.2).

    Attributes:
        text: Weight for candidates from the naive text channel.
        graph: Weight for candidates from the graph channel.
        audio: Weight for candidates from the audio channel.
    """

    text: float = 0.5
    graph: float = 0.3
    audio: float = 0.2

    def __post_init__(self) -> None:
        total = self.text + self.graph + self.audio
        if not 0.99 <= total <= 1.01:
            raise ValueError(
                f"ChannelWeights must sum to ~1.0 (got {total:.4f}; "
                f"text={self.text} graph={self.graph} audio={self.audio})"
            )
        for label, value in (
            ("text", self.text),
            ("graph", self.graph),
            ("audio", self.audio),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"ChannelWeights.{label} must be in [0.0, 1.0], got {value}")

    @property
    def total(self) -> float:
        """Sum of the three weights (for diagnostics)."""
        return self.text + self.graph + self.audio

    def normalised_for_disabled_audio(self) -> ChannelWeights:
        """Return a new ChannelWeights with audio=0 and (text, graph) renormalised.

        Used when ``enable_voiceprint=False`` so that audio-disabled callers
        do not silently lose 20% of total score magnitude. Implements the
        rule from architecture §10.2 (footnote):

            (0.5, 0.3, 0.0) → (0.625, 0.375, 0.0)

        If ``audio`` is already 0, the object is returned unchanged (it is
        already normalised). If ``text+graph`` is 0, returns the object
        unchanged (defensive — caller passed a degenerate config).
        """
        if self.audio == 0.0:
            return self
        denom = self.text + self.graph
        if denom <= 0.0:
            return self
        return ChannelWeights(text=self.text / denom, graph=self.graph / denom, audio=0.0)

    def weight_for(self, source_channel: str) -> float:
        """Look up the weight for a candidate's ``source_channel``.

        Unknown channels (e.g. legacy candidates without a tagged channel)
        default to the text weight (preserves M3-M6 behaviour).
        """
        mapping = {
            "naive": self.text,
            "graph": self.graph,
            "audio": self.audio,
        }
        return mapping.get(source_channel, self.text)


# AMBIGUOUS speaker candidate downgrade factor (§10.3 / §17.6).
AMBIGUOUS_SPEAKER_PENALTY: float = 0.7


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
        file_index: Deprecated compatibility argument. LLM results are never
            read from or written to FileIndex; the centralized gateway owns
            all result caching.
        graph_store: Optional NetworkXGraphStore for entity/confidence lookup.
        channel_weights: M7 rerank fusion weights (Q1 locked: 0.5 / 0.3 / 0.2).
            When ``None``, defaults to ``ChannelWeights()``. Callers that
            disable the audio channel should pass
            ``ChannelWeights().normalised_for_disabled_audio()`` (or rely
            on ``disable_audio_channel()`` to do it automatically).
        enable_batch_judge: Opt-in quality-gated batch relevance judging.
            Defaults to ``False`` so the established per-candidate path is
            unchanged until an external gold-set gate approves batching.
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        *,
        file_index: FileIndex | None = None,
        graph_store: NetworkXGraphStore | None = None,
        channel_weights: ChannelWeights | None = None,
        enable_batch_judge: bool = False,
    ) -> None:
        self._bundle = bundle
        if file_index is not None:
            logger.debug("Reranker FileIndex LLM cache is disabled; using LLMGateway")
        self._graph_store = graph_store
        self._channel_weights = channel_weights or ChannelWeights()
        self._enable_batch_judge = enable_batch_judge

    @property
    def channel_weights(self) -> ChannelWeights:
        """Current channel weights (read-only view)."""
        return self._channel_weights

    def disable_audio_channel(self) -> None:
        """Mutate the channel weights to drop the audio channel.

        Convenience for ``enable_voiceprint=False`` callers: re-normalises
        text+graph so total = 1.0. After this call, ``channel_weights.audio``
        is 0.0 and ``(text, graph)`` are scaled up to compensate.

        Idempotent — calling twice is a no-op.
        """
        self._channel_weights = self._channel_weights.normalised_for_disabled_audio()

    def _weighted_score(self, candidate: CandidateSegment) -> float:
        """Compute the channel-weighted fusion score for one candidate.

        - channel weight × candidate.score (base fusion).
        - AMBIGUOUS speaker affiliation → score × 0.7 (§10.3). Detection
          is delegated to ``_candidate_from_ambiguous_speaker`` (returns
          ``False`` when ``graph_store`` is ``None``).

        Args:
            candidate: One retrieval candidate.

        Returns:
            Fused score (float). Lower-is-better callers should negate.
        """
        weight = self._channel_weights.weight_for(candidate.source_channel)
        score = weight * candidate.score
        if self._candidate_from_ambiguous_speaker(candidate):
            score *= AMBIGUOUS_SPEAKER_PENALTY
        return score

    def _candidate_from_ambiguous_speaker(self, candidate: CandidateSegment) -> bool:
        """Heuristic: does this candidate's chunk relate to an AMBIGUOUS speaker?

        Conservative synchronous check against the in-memory ``graph_store``.
        When the graph store is absent or the lookup fails for any reason,
        returns ``False`` (no penalty applied — we never penalise what we
        cannot verify).

        Subclasses / future M8 work can override this with a real DB hit
        to ``speaker_nodes`` joined via ``chunk_id`` → ``recordings.id`` →
        ``speaker_links.recording_id``.
        """
        if self._graph_store is None:
            return False
        try:
            # NetworkXGraphStore caches graph in memory; the check is cheap.
            # We probe by source_id format "{recording_id}_{chunk_id}".
            source_id = f"{candidate.recording_id}_{candidate.chunk_id}"
            # ``get_node`` is async — but the canonical SPEAKER node lookup
            # is performed via ``get_all_nodes`` which already returns a list.
            # For the synchronous path we use the cached NetworkX ``graph``
            # directly when available.
            graph = getattr(self._graph_store, "graph", None)
            if graph is None:
                return False
            for _node_id, attrs in graph.nodes(data=True):
                if attrs.get("type") != "SPEAKER":
                    continue
                if attrs.get("ambiguity_tag") != "AMBIGUOUS":
                    continue
                # SPEAKER node source_ids is a JSON-encoded list of
                # ``{recording_id}_{chunk_id}`` strings.
                raw_sources = attrs.get("source_ids", "[]")
                if isinstance(raw_sources, (list, tuple)):
                    if source_id in raw_sources:
                        return True
                elif isinstance(raw_sources, str) and source_id in raw_sources:
                    return True
        except Exception as exc:
            logger.debug("AMBIGUOUS speaker probe failed for chunk %d: %s", candidate.chunk_id, exc)
        return False

    def rank_candidates(
        self,
        candidates: Sequence[CandidateSegment],
    ) -> list[CandidateSegment]:
        """Sort candidates by channel-weighted fusion score (descending).

        Pure synchronous helper — exposed so rerank + retrieval can share
        the same ranking logic without going through the full LLM-judge
        pipeline. AMBIGUOUS speaker candidates are kept (downweighted, not
        removed — §17.6).

        Args:
            candidates: Retrieval candidates (any source_channel mix).

        Returns:
            New list sorted by ``_weighted_score`` descending.
        """
        return sorted(
            candidates,
            key=self._weighted_score,
            reverse=True,
        )

    async def rerank_and_answer(
        self,
        query: str,
        candidates: Sequence[CandidateSegment],
        *,
        time_range: tuple[datetime, datetime] | None = None,
        keywords: Sequence[str] | None = None,
        tenant_id: str = "default",
        permission_scope: Mapping[str, Any] | None = None,
    ) -> RerankResult:
        """Execute the full rerank + answer pipeline.

        Args:
            query: User query.
            candidates: Retrieval candidates.
            time_range: Optional time range (for context in answer generation).
            keywords: Keywords already extracted by the retriever. When
                provided, including an empty sequence, no second LLM keyword
                extraction is performed.
            tenant_id: Tenant scope for cache and provider isolation.
            permission_scope: Authorization scope that constrains result reuse.

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
        resolved_scope = _resolved_permission_scope(tenant_id, permission_scope)
        surviving, filtered_count = await self._llm_judge_filter(
            query,
            candidates,
            tenant_id=tenant_id,
            permission_scope=resolved_scope,
        )

        # Step 2: Keyword extraction
        resolved_keywords = (
            list(keywords)
            if keywords is not None
            else await self._extract_keywords(
                query,
                tenant_id=tenant_id,
                permission_scope=resolved_scope,
            )
        )

        # Step 3: Refined reranking
        refined = await self._refine_descriptions(surviving, resolved_keywords)

        # Step 4: Build citations
        citations = await self._build_citations(refined)

        # Step 5: Answer generation
        answer = await self._generate_answer(
            query,
            refined,
            citations,
            time_range,
            tenant_id=tenant_id,
            permission_scope=resolved_scope,
        )

        return RerankResult(
            answer=answer,
            citations=citations,
            filtered_count=filtered_count,
            refined_count=len(refined),
        )

    # ------------------------------------------------------------------
    # LLM as-judge filter
    # ------------------------------------------------------------------

    def _relevance_request(
        self,
        query: str,
        candidate: CandidateSegment,
        *,
        tenant_id: str,
        permission_scope: Mapping[str, Any] | None,
    ) -> LLMRequest:
        """Build the canonical per-candidate recipe used by both judge paths."""

        adapter = self._bundle.strong_llm
        query_hash = _query_sha256(query)
        return LLMRequest(
            tenant_id=tenant_id,
            purpose="relevance_judge",
            model_tier="strong",
            provider=str(getattr(adapter, "provider", "openai-compatible")),
            model_epoch=str(getattr(adapter, "model_epoch", adapter.model)),
            messages=(
                {
                    "role": "system",
                    "content": "判断录音段是否与问题相关，只回答 yes 或 no。",
                },
                {
                    "role": "user",
                    "content": (f"问题: {query}\n段文本: {candidate.text[:500]}"),
                },
            ),
            prompt_version=_RELEVANCE_PROMPT_VERSION,
            schema_version=_RELEVANCE_SCHEMA_VERSION,
            parser_version=_RELEVANCE_PARSER_VERSION,
            postprocessor_version=_RELEVANCE_POSTPROCESSOR_VERSION,
            temperature=0.0,
            top_p=1.0,
            response_schema={"type": "string", "enum": ["yes", "no"]},
            business_snapshot={
                "query_sha256": query_hash,
                "candidate": _candidate_snapshot(candidate),
            },
            permission_scope=_resolved_permission_scope(
                tenant_id,
                permission_scope,
            ),
            provenance=_request_provenance(query_hash, (candidate,)),
            cache_policy=CachePolicy.EXACT,
            ttl_seconds=_RELEVANCE_TTL_SECONDS,
            response_validator=_valid_yes_no_response,
        )

    async def _llm_judge_filter(
        self,
        query: str,
        candidates: Sequence[CandidateSegment],
        *,
        tenant_id: str = "default",
        permission_scope: Mapping[str, Any] | None = None,
    ) -> tuple[list[CandidateSegment], int]:
        """Filter candidates by LLM as-judge (yes/no relevance).

        Conservative strategy: if LLM judge fails, KEEP the candidate
        (prefer false positives over false negatives).

        Args:
            query: User query.
            candidates: All retrieval candidates.
            tenant_id: Tenant scope for cache isolation.
            permission_scope: Authorization scope that constrains reuse.

        Returns:
            Tuple of (surviving_candidates, filtered_count).
        """
        if self._enable_batch_judge:
            return await self._llm_judge_filter_batch(
                query,
                candidates,
                tenant_id=tenant_id,
                permission_scope=permission_scope,
            )

        surviving: list[CandidateSegment] = []
        filtered_count = 0

        for cand in candidates:
            try:
                request = self._relevance_request(
                    query,
                    cand,
                    tenant_id=tenant_id,
                    permission_scope=permission_scope,
                )
                adapter = self._bundle.strong_llm
                response = await execute_llm(adapter, request)

                if _yes_no_verdict(response) == "no":
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

    @staticmethod
    def _batch_candidate_ids(candidates: Sequence[CandidateSegment]) -> list[str]:
        """Build compact, deterministic and unique ids for one ordered batch."""

        occurrences: dict[str, int] = {}
        candidate_ids: list[str] = []
        for candidate in candidates:
            base = f"recording:{candidate.recording_id}/chunk:{candidate.chunk_id}"
            occurrence = occurrences.get(base, 0) + 1
            occurrences[base] = occurrence
            candidate_ids.append(base if occurrence == 1 else f"{base}/occurrence:{occurrence}")
        return candidate_ids

    async def _llm_judge_filter_batch(
        self,
        query: str,
        candidates: Sequence[CandidateSegment],
        *,
        tenant_id: str = "default",
        permission_scope: Mapping[str, Any] | None = None,
    ) -> tuple[list[CandidateSegment], int]:
        """Reuse per-item verdicts and batch only exact-cache misses.

        Candidate order is never derived from cache completion order. Invalid
        cache values become misses; invalid or failed batch verdicts are kept
        conservatively.
        """

        if not candidates:
            return [], 0
        adapter = self._bundle.strong_llm
        candidate_ids = self._batch_candidate_ids(candidates)
        individual_requests = [
            self._relevance_request(
                query,
                candidate,
                tenant_id=tenant_id,
                permission_scope=permission_scope,
            )
            for candidate in candidates
        ]
        resolved_verdicts: list[str | None] = [None] * len(candidates)
        miss_indexes: list[int] = []
        for index, request in enumerate(individual_requests):
            try:
                cached = await lookup_llm_cache(adapter, request)
            except Exception as exc:
                logger.warning(
                    "Per-candidate relevance cache lookup failed for chunk %d: %s",
                    candidates[index].chunk_id,
                    exc,
                )
                cached = None
            verdict = _yes_no_verdict(cached) if cached is not None else None
            if verdict is None:
                miss_indexes.append(index)
            else:
                resolved_verdicts[index] = verdict

        if miss_indexes:
            miss_ids = [candidate_ids[index] for index in miss_indexes]
            miss_candidates = [candidates[index] for index in miss_indexes]
            query_hash = _query_sha256(query)
            output_schema: dict[str, Any] = {
                "type": "object",
                "additionalProperties": False,
                "required": ["verdicts"],
                "properties": {
                    "verdicts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["candidate_id", "verdict", "reason"],
                            "properties": {
                                "candidate_id": {
                                    "type": "string",
                                    "enum": miss_ids,
                                },
                                "verdict": {
                                    "type": "string",
                                    "enum": ["yes", "no"],
                                },
                                "reason": {"type": "string"},
                            },
                        },
                    }
                },
            }
            output_contract = {
                "verdicts": [
                    {
                        "candidate_id": "one supplied candidate_id",
                        "verdict": "yes|no",
                        "reason": "string",
                    }
                ]
            }
            request_payload = {
                "query": query,
                "candidates": [
                    {
                        "candidate_id": candidate_id,
                        "text": candidate.text[:500],
                    }
                    for candidate_id, candidate in zip(
                        miss_ids,
                        miss_candidates,
                        strict=True,
                    )
                ],
            }
            messages: tuple[dict[str, str], ...] = (
                {
                    "role": "system",
                    "content": (
                        "逐项判断候选录音段是否与问题相关。"
                        "必须只返回严格 JSON，不得遗漏候选，不得重复 candidate_id。"
                        "输出契约："
                        + json.dumps(
                            output_contract,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        request_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            )
            batch_request = LLMRequest(
                tenant_id=tenant_id,
                purpose="relevance_judge",
                model_tier="strong",
                provider=str(getattr(adapter, "provider", "openai-compatible")),
                model_epoch=str(getattr(adapter, "model_epoch", adapter.model)),
                messages=messages,
                prompt_version=_RELEVANCE_PROMPT_VERSION,
                schema_version=_BATCH_RELEVANCE_SCHEMA_VERSION,
                parser_version=_BATCH_RELEVANCE_PARSER_VERSION,
                postprocessor_version=_RELEVANCE_POSTPROCESSOR_VERSION,
                temperature=0.0,
                top_p=1.0,
                response_format={"type": "json_object"},
                response_schema=output_schema,
                business_snapshot={
                    "query_sha256": query_hash,
                    "candidate_ids": miss_ids,
                    "candidates": [_candidate_snapshot(candidate) for candidate in miss_candidates],
                },
                permission_scope=_resolved_permission_scope(
                    tenant_id,
                    permission_scope,
                ),
                provenance=_request_provenance(query_hash, miss_candidates),
                cache_policy=CachePolicy.EXACT,
                ttl_seconds=_RELEVANCE_TTL_SECONDS,
                response_validator=lambda response: _valid_batch_response(
                    response,
                    miss_ids,
                ),
            )
            try:
                response = await execute_llm(adapter, batch_request)
                payload = json.loads(response.text)
                if not isinstance(payload, dict) or set(payload) != {"verdicts"}:
                    raise ValueError("batch judge response must contain only verdicts")
                raw_verdicts = payload["verdicts"]
                if not isinstance(raw_verdicts, list):
                    raise ValueError("batch judge verdicts must be a list")

                known_ids = set(miss_ids)
                seen: dict[str, int] = {}
                duplicate_ids: set[str] = set()
                valid_verdicts: dict[str, str] = {}
                for raw in raw_verdicts:
                    if not isinstance(raw, dict):
                        continue
                    candidate_id = raw.get("candidate_id")
                    if not isinstance(candidate_id, str) or candidate_id not in known_ids:
                        continue
                    seen[candidate_id] = seen.get(candidate_id, 0) + 1
                    if seen[candidate_id] > 1:
                        duplicate_ids.add(candidate_id)
                        valid_verdicts.pop(candidate_id, None)
                        continue
                    if set(raw) != {"candidate_id", "verdict", "reason"}:
                        continue
                    verdict = raw.get("verdict")
                    reason = raw.get("reason")
                    if verdict not in {"yes", "no"} or not isinstance(reason, str):
                        continue
                    valid_verdicts[candidate_id] = verdict

                for miss_position, (index, candidate_id) in enumerate(
                    zip(miss_indexes, miss_ids, strict=True)
                ):
                    verdict = (
                        None if candidate_id in duplicate_ids else valid_verdicts.get(candidate_id)
                    )
                    if verdict is None:
                        continue
                    resolved_verdicts[index] = verdict
                    derived_response = LLMResponse(
                        text=verdict,
                        model=response.model,
                        prompt_hash=individual_requests[index].recipe_sha256(
                            model=adapter.model,
                        ),
                        cached=False,
                        usage=_batch_usage_share(
                            response.usage,
                            part_index=miss_position,
                            part_count=len(miss_indexes),
                        ),
                        cache_source="batch_derived",
                        provider_called=response.provider_called,
                    )
                    try:
                        await store_validated_llm_cache(
                            adapter,
                            individual_requests[index],
                            derived_response,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Per-candidate relevance cache write failed for chunk %d: %s",
                            candidates[index].chunk_id,
                            exc,
                        )
            except Exception as exc:
                logger.warning(
                    "Batch LLM judge failed; conservatively keeping %d misses: %s",
                    len(miss_indexes),
                    exc,
                )

        surviving: list[CandidateSegment] = []
        filtered_count = 0
        for verdict, candidate in zip(resolved_verdicts, candidates, strict=True):
            if verdict == "no":
                filtered_count += 1
            else:
                surviving.append(candidate)
        return surviving, filtered_count

    # ------------------------------------------------------------------
    # Keyword extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_keywords(text: str) -> list[str]:
        """Parse the exact delimiter format accepted by keyword extraction."""

        import re

        normalized = re.sub(
            r"^(关键词|keywords?)[:：]?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        parts = re.split(r"[,，;；\n、]+", normalized)
        return [part.strip() for part in parts if len(part.strip()) >= 2]

    async def _extract_keywords(
        self,
        query: str,
        *,
        tenant_id: str = "default",
        permission_scope: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Extract keywords from query via weak_llm.

        Falls back to simple split if LLM fails.

        Args:
            query: User query.
            tenant_id: Tenant scope for cache isolation.
            permission_scope: Authorization scope that constrains reuse.

        Returns:
            List of keyword strings.
        """
        import re

        try:
            messages: list[dict[str, str]] = [
                {
                    "role": "system",
                    "content": "从用户问题中提取关键词，只用逗号分隔返回关键词。",
                },
                {"role": "user", "content": query},
            ]
            adapter = self._bundle.weak_llm
            query_hash = _query_sha256(query)
            semantic_language = detect_semantic_language(query)
            request = LLMRequest(
                tenant_id=tenant_id,
                purpose="keyword_extract",
                model_tier="weak",
                provider=str(getattr(adapter, "provider", "openai-compatible")),
                model_epoch=str(getattr(adapter, "model_epoch", adapter.model)),
                messages=messages,
                prompt_version=_KEYWORD_PROMPT_VERSION,
                schema_version=_KEYWORD_SCHEMA_VERSION,
                parser_version=_KEYWORD_PARSER_VERSION,
                postprocessor_version=_KEYWORD_POSTPROCESSOR_VERSION,
                temperature=0.0,
                top_p=1.0,
                response_schema={
                    "type": "string",
                    "description": "Comma-separated query keywords",
                },
                business_snapshot={
                    "query_sha256": query_hash,
                    "language": semantic_language,
                },
                permission_scope=_resolved_permission_scope(
                    tenant_id,
                    permission_scope,
                ),
                semantic_text=query,
                semantic_language=semantic_language,
                semantic_protected_values=semantic_protected_identifiers(query),
                provenance=_request_provenance(query_hash),
                cache_policy=CachePolicy.QUERY_SEMANTIC,
                ttl_seconds=_QUERY_HELPER_TTL_SECONDS,
                response_validator=lambda response: bool(self._parse_keywords(response.text)),
            )
            response = await execute_llm(adapter, request)

            keywords = self._parse_keywords(response.text)
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
        *,
        tenant_id: str = "default",
        permission_scope: Mapping[str, Any] | None = None,
    ) -> str:
        """Generate final answer via strong_llm.

        Args:
            query: User query.
            candidates: Refined candidate segments.
            citations: Provenance citations.
            time_range: Optional time range context.
            tenant_id: Tenant scope for cache isolation.
            permission_scope: Authorization scope that constrains reuse.

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

        prompt = f"{time_context}问题: {query}\n\n相关录音段:\n{context}\n\n请作答。"

        try:
            messages: list[dict[str, str]] = [
                {
                    "role": "system",
                    "content": (
                        "仅依据给定录音证据回答问题；不得臆测，并在引用处标注序号 [1] [2] 等。"
                    ),
                },
                {"role": "user", "content": prompt},
            ]
            adapter = self._bundle.strong_llm
            query_hash = _query_sha256(query)
            request = LLMRequest(
                tenant_id=tenant_id,
                purpose="final_answer",
                model_tier="strong",
                provider=str(getattr(adapter, "provider", "openai-compatible")),
                model_epoch=str(getattr(adapter, "model_epoch", adapter.model)),
                messages=messages,
                prompt_version=_FINAL_ANSWER_PROMPT_VERSION,
                schema_version=_FINAL_ANSWER_SCHEMA_VERSION,
                parser_version=_FINAL_ANSWER_PARSER_VERSION,
                postprocessor_version=_FINAL_ANSWER_POSTPROCESSOR_VERSION,
                temperature=0.0,
                top_p=1.0,
                response_schema={
                    "type": "string",
                    "description": "Grounded answer with inline numeric citations",
                },
                business_snapshot={
                    "query_sha256": query_hash,
                    "time_range": (
                        [time_range[0].isoformat(), time_range[1].isoformat()]
                        if time_range is not None
                        else None
                    ),
                    "evidence": [
                        {
                            **_candidate_snapshot(candidate),
                            "entity": citation.entity,
                            "confidence": citation.confidence,
                        }
                        for candidate, citation in zip(candidates, citations, strict=True)
                    ],
                },
                permission_scope=_resolved_permission_scope(
                    tenant_id,
                    permission_scope,
                ),
                provenance=_request_provenance(query_hash, candidates),
                cache_policy=CachePolicy.EXACT,
                ttl_seconds=_FINAL_ANSWER_TTL_SECONDS,
                response_validator=_valid_final_answer,
            )
            response = await execute_llm(adapter, request)
            return response.text
        except Exception as exc:
            logger.warning("Answer generation failed: %s", exc)
            return "（生成失败）"


# ============================================================
# Confidence ranking helper
# ============================================================

_CONFIDENCE_RANK: dict[str, int] = {
    "DEPRECATED": -1,
    "AMBIGUOUS": 0,
    "INFERRED": 1,
    "EXTRACTED": 2,
}


def _confidence_rank(confidence: str) -> int:
    """Return numeric rank for confidence (higher = better)."""
    return _CONFIDENCE_RANK.get(confidence, 0)


__all__ = [
    "AMBIGUOUS_SPEAKER_PENALTY",
    "ChannelWeights",
    "Citation",
    "RerankResult",
    "Reranker",
]
