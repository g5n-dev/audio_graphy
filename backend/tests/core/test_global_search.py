"""Unit tests for T8 — GlobalSearcher (core/global_search.py).

Coverage:
    - Empty input → empty result.
    - top_k cap is respected.
    - Concurrency cap = 5 enforced (verified by timing).
    - Default keyword scorer picks obviously-relevant summaries first.
    - L4 constants exposed.
    - Invalid top_k / concurrency raise ValueError.
    - community_ids allow-list filters correctly.
    - Level out of range raises ValueError.
"""

from __future__ import annotations

import asyncio

import pytest

from audio_graphy.core.community_summary import CommunitySummaryRecord
from audio_graphy.core.global_search import (
    L4_MAX_CONCURRENCY,
    L4_TOP_K_DEFAULT,
    GlobalSearcher,
    _tokenize,
    default_keyword_scorer,
)


def _rec(
    community_id: int,
    title: str,
    summary: str,
    level: int = 0,
    member_count: int = 3,
) -> CommunitySummaryRecord:
    from datetime import UTC, datetime

    return CommunitySummaryRecord(
        leiden_job_id=1,
        level=level,
        community_id=community_id,
        title=title,
        summary=summary,
        member_count=member_count,
        member_node_ids=[f"e{i}" for i in range(member_count)],
        generated_at=datetime.now(UTC),
        strategy="eager",
    )


# ============================================================
# Constants + construction
# ============================================================


def test_l4_constants_match_ruling():
    assert L4_TOP_K_DEFAULT == 5
    assert L4_MAX_CONCURRENCY == 5


def test_invalid_top_k_raises():
    with pytest.raises(ValueError):
        GlobalSearcher(provider=lambda **kw: [], top_k=0)
    with pytest.raises(ValueError):
        GlobalSearcher(provider=lambda **kw: [], top_k=999)


def test_invalid_concurrency_raises():
    with pytest.raises(ValueError):
        GlobalSearcher(provider=lambda **kw: [], max_concurrency=0)
    with pytest.raises(ValueError):
        GlobalSearcher(provider=lambda **kw: [], max_concurrency=999)


# ============================================================
# Empty input
# ============================================================


@pytest.mark.asyncio
async def test_empty_provider_returns_empty_result():
    searcher = GlobalSearcher(provider=lambda **kw: [])
    result = await searcher.search(query="anything", tenant_id="t1", level=0)
    assert result.hits == []
    assert result.total == 0
    assert result.took_ms >= 0.0


# ============================================================
# top_k cap
# ============================================================


@pytest.mark.asyncio
async def test_top_k_is_respected():
    recs = [_rec(i, f"社区 {i}", f"关键词 {i}") for i in range(20)]
    searcher = GlobalSearcher(provider=lambda **kw: recs, top_k=5)
    result = await searcher.search(query="关键词", tenant_id="t1", level=0)
    assert len(result.hits) == 5


# ============================================================
# Keyword scorer ordering
# ============================================================


@pytest.mark.asyncio
async def test_keyword_scorer_ranks_relevant_first():
    recs = [
        _rec(1, "其他主题", "毫不相关的描述"),
        _rec(2, "长安汽车", "客户询问 长安CS75 价格方案"),
        _rec(3, "比亚迪", "客户对比 比亚迪汉 与 长安CS75"),
    ]
    searcher = GlobalSearcher(provider=lambda **kw: recs, top_k=3)
    result = await searcher.search(query="长安CS75", tenant_id="t1", level=0)
    # Community 2 and 3 both mention 长安CS75; community 1 doesn't.
    top_ids = [h.community_id for h in result.hits]
    assert 1 not in top_ids or top_ids[-1] == 1
    assert top_ids[0] in {2, 3}


@pytest.mark.asyncio
async def test_default_scorer_zero_on_no_overlap():
    r = _rec(1, "abc", "def")
    score = await default_keyword_scorer(query="xyz", summary=r)
    assert score == 0.0


# ============================================================
# Concurrency cap
# ============================================================


@pytest.mark.asyncio
async def test_concurrency_does_not_exceed_cap():
    """Each scorer call records the in-flight count; assert ≤5 concurrent."""
    state: dict[str, int] = {"active": 0, "max": 0}

    async def slow_scorer(*, query: str, summary: CommunitySummaryRecord) -> float:
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return float(summary.community_id) / 10.0

    recs = [_rec(i, f"社区 {i}", f"摘要 {i}") for i in range(30)]
    searcher = GlobalSearcher(
        provider=lambda **kw: recs,
        scorer=slow_scorer,  # type: ignore[arg-type]
        top_k=10,
        max_concurrency=L4_MAX_CONCURRENCY,
    )
    result = await searcher.search(query="x", tenant_id="t1", level=0)
    assert state["max"] <= L4_MAX_CONCURRENCY
    assert len(result.hits) == 10


# ============================================================
# community_ids allow-list
# ============================================================


@pytest.mark.asyncio
async def test_community_ids_filter():
    recs = [_rec(i, f"社区 {i}", f"摘要 {i}") for i in range(10)]
    searcher = GlobalSearcher(
        provider=lambda **kw: recs, top_k=10
    )
    result = await searcher.search(
        query="社区",
        tenant_id="t1",
        level=0,
        community_ids=[2, 4, 6],
    )
    assert {h.community_id for h in result.hits} == {2, 4, 6}


# ============================================================
# Level range
# ============================================================


@pytest.mark.asyncio
async def test_level_out_of_range_raises():
    searcher = GlobalSearcher(provider=lambda **kw: [])
    with pytest.raises(ValueError):
        await searcher.search(query="x", tenant_id="t1", level=3)
    with pytest.raises(ValueError):
        await searcher.search(query="x", tenant_id="t1", level=-1)


# ============================================================
# Tokenizer sanity
# ============================================================


def test_tokenizer_handles_chinese_bigrams():
    tokens = _tokenize("客户询问 长安CS75")
    # CJK bigrams present.
    assert any(t == "客户" for t in tokens)
    assert any(t == "询问" for t in tokens)
    # The whitespace-split token "长安CS75" is captured as-is.
    assert "长安cs75" in tokens


def test_tokenizer_handles_empty_string():
    assert _tokenize("") == set()
