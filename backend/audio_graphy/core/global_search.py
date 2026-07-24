"""T8 — GlobalSearcher (M9 architecture §10, L4 map-reduce ruling).

GraphRAG-style global search that runs a map-reduce over community
summaries. Per L4 binding rulings:

    - top_k            = 5 (default; max 50)
    - concurrency      = ≤ 5 concurrent map calls (asyncio.Semaphore)
    - community filter = optional allow-list of community ids at ``level``
    - level            = 0..2 only (Q2 cap; level 3 was dropped upstream)

The searcher is storage-agnostic: callers pass in a list of
``CommunitySummaryRecord``-like dicts plus an optional LLM scorer.
A simple keyword-overlap scorer is the default fallback so the service
is unit-testable without a real LLM.

Attribution: the map-reduce-over-community-summaries pattern follows
GraphRAG (Microsoft, 2024) — MIT-clean conceptual reference.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from audio_graphy.api.metrics import GLOBAL_SEARCH_DURATION
from audio_graphy.core.community_summary import CommunitySummaryRecord

logger = logging.getLogger(__name__)


# L4 binding constants.
L4_TOP_K_DEFAULT: int = 5
L4_MAX_CONCURRENCY: int = 5


# ============================================================
# Protocols
# ============================================================


class CommunitySummaryProvider(Protocol):
    """Callable that returns community summaries for a tenant + level."""

    def __call__(self, *, tenant_id: str, level: int) -> list[CommunitySummaryRecord]: ...


class LLMScorer(Protocol):
    """Async callable returning a 0..1 relevance score for one summary."""

    async def __call__(self, *, query: str, summary: CommunitySummaryRecord) -> float: ...


# ============================================================
# Default scorer — keyword-overlap fallback
# ============================================================


def _tokenize(s: str) -> set[str]:
    """Tiny CJK-aware tokenizer.

    Splits on whitespace + punctuation; also yields character bigrams for
    CJK ranges so Chinese queries match Chinese summaries even when
    neither side has whitespace tokens.
    """
    out: set[str] = set()
    chunks = [c for c in s if c.isalnum()]
    # Whitespace tokens.
    for tok in s.lower().split():
        if tok:
            out.add(tok)
    # CJK bigrams.
    cjk: list[str] = []
    for c in s:
        if "\u4e00" <= c <= "\u9fff":
            cjk.append(c)
        else:
            if len(cjk) >= 2:
                for i in range(len(cjk) - 1):
                    out.add("".join(cjk[i : i + 2]))
            cjk.clear()
    if len(cjk) >= 2:
        for i in range(len(cjk) - 1):
            out.add("".join(cjk[i : i + 2]))
    # Single alphanumeric chunks (fallback).
    for c in chunks:
        if c.isalpha() or c.isdigit():
            out.add(c.lower())
    return out


async def default_keyword_scorer(*, query: str, summary: CommunitySummaryRecord) -> float:
    """Default CommunityHit scorer — keyword overlap (Jaccard).

    Returns ``|q ∩ s| / |q ∪ s|`` over character bigrams + word tokens.
    """
    q = _tokenize(query)
    s = _tokenize(summary.title + " " + summary.summary)
    if not q or not s:
        return 0.0
    inter = len(q & s)
    union = len(q | s)
    return inter / union if union else 0.0


# ============================================================
# Service
# ============================================================


@dataclass(frozen=True, slots=True)
class GlobalSearchHit:
    """One scored community summary inside a global-search result.

    Attributes:
        community_id: Leiden-assigned integer id at ``level``.
        level: Hierarchy depth (0..2).
        title: Summary title.
        summary: Full summary text.
        score: Relevance score in [0, 1].
        member_count: Members at write time.
    """

    community_id: int
    level: int
    title: str
    summary: str
    score: float
    member_count: int


@dataclass(frozen=True, slots=True)
class GlobalSearchResult:
    """Aggregated output of one ``GlobalSearcher.search`` call."""

    query: str
    level: int
    hits: list[GlobalSearchHit]
    took_ms: float

    @property
    def total(self) -> int:
        return len(self.hits)


class GlobalSearcher:
    """Map-reduce over community summaries (L4 top-k=5, concurrency ≤5).

    Args:
        provider: Returns all candidate summaries for ``(tenant_id, level)``.
        scorer: Async callable returning [0, 1] relevance for one summary.
            Defaults to ``default_keyword_scorer``.
        top_k: Result cap (default 5 per L4).
        max_concurrency: Concurrent map tasks (default 5 per L4).
    """

    def __init__(
        self,
        *,
        provider: CommunitySummaryProvider,
        scorer: LLMScorer | None = None,
        top_k: int = L4_TOP_K_DEFAULT,
        max_concurrency: int = L4_MAX_CONCURRENCY,
    ) -> None:
        if top_k <= 0 or top_k > 50:
            raise ValueError(f"top_k out of range: {top_k}")
        if max_concurrency <= 0 or max_concurrency > L4_MAX_CONCURRENCY:
            raise ValueError(
                f"max_concurrency must be in [1, {L4_MAX_CONCURRENCY}], got {max_concurrency}"
            )
        self._provider = provider
        self._scorer: LLMScorer = scorer or _DefaultLLMScorer()
        self._top_k = top_k
        self._sem = asyncio.Semaphore(max_concurrency)

    async def search(
        self,
        *,
        query: str,
        tenant_id: str,
        level: int = 0,
        community_ids: Sequence[int] | None = None,
    ) -> GlobalSearchResult:
        """Run the map-reduce.

        Step 1: provider returns all summaries at ``level``.
        Step 2: optional allow-list filter on ``community_ids``.
        Step 3: map — score each summary (capped at ``max_concurrency``).
        Step 4: reduce — top-k sorted desc by score.
        """
        started = time.perf_counter()
        if level < 0 or level > 2:
            raise ValueError(f"level out of range (Q2 cap=2): {level}")

        candidates = self._provider(tenant_id=tenant_id, level=level)
        if community_ids is not None:
            allow = set(community_ids)
            candidates = [c for c in candidates if c.community_id in allow]

        async def _score_one(rec: CommunitySummaryRecord) -> GlobalSearchHit:
            async with self._sem:
                score = await self._scorer(query=query, summary=rec)
                return GlobalSearchHit(
                    community_id=rec.community_id,
                    level=rec.level,
                    title=rec.title,
                    summary=rec.summary,
                    score=float(score),
                    member_count=int(rec.member_count),
                )

        hits = await asyncio.gather(*(_score_one(c) for c in candidates))
        hits.sort(key=lambda h: h.score, reverse=True)
        top = hits[: self._top_k]
        elapsed = time.perf_counter() - started
        # L4 — observe global-search latency (M9 §17 PRD-listed metric).
        try:
            GLOBAL_SEARCH_DURATION.observe(elapsed)
        except Exception:  # pragma: no cover — defensive
            logger.debug("GLOBAL_SEARCH_DURATION observe failed", exc_info=True)
        return GlobalSearchResult(
            query=query,
            level=level,
            hits=top,
            took_ms=elapsed * 1000.0,
        )


# ============================================================
# Default scorer wrapper (so the protocol is awaitable)
# ============================================================


@dataclass(frozen=True, slots=True)
class _DefaultLLMScorer:
    """Adapter wrapping ``default_keyword_scorer`` to satisfy LLMScorer."""

    weight_title: float = 1.0
    weight_summary: float = 1.0

    async def __call__(self, *, query: str, summary: CommunitySummaryRecord) -> float:
        return await default_keyword_scorer(query=query, summary=summary)


__all__ = [
    "L4_MAX_CONCURRENCY",
    "L4_TOP_K_DEFAULT",
    "CommunitySummaryProvider",
    "GlobalSearchHit",
    "GlobalSearchResult",
    "GlobalSearcher",
    "LLMScorer",
    "default_keyword_scorer",
]
