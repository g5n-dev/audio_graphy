"""T7 — CommunitySummaryService tests (architecture §8, Q2).

Verifies Q2 ruling:
  - level 0 + leaf communities → eager (always generated)
  - levels 1-2 → lazy (generated on first retrieval)
  - level 3 → dropped (never present in memberships)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from audio_graphy.core.community_summary import (
    Q2_MAX_LEVEL,
    CommunitySummaryRecord,
    CommunitySummaryService,
    InMemorySummarySink,
    _parse_llm_output,
    load_default_prompt,
)
from audio_graphy.core.leiden import LeidenRunResult
from audio_graphy.core.types import GraphEdge, GraphNode

# ============================================================
# Fakes
# ============================================================


class _FakeLLM:
    """Returns a deterministic TITLE/SUMMARY pair."""

    def __init__(self, title: str = "Test Title", summary: str = "Test Summary") -> None:
        self.title = title
        self.summary = summary
        self.calls: list[str] = []

    async def complete(self, prompt: str, *, max_tokens: int = 256) -> str:
        self.calls.append(prompt)
        return f"TITLE: {self.title}\nSUMMARY: {self.summary}"


def _node(eid: str, t: str = "车型") -> GraphNode:
    return GraphNode(
        entity_id=eid,
        name=eid,
        type=t,
        description=f"desc_{eid}",
        source_ids=[],
        recording_ids=[1],
    )


def _edge(s: str, t: str) -> GraphEdge:
    return GraphEdge(
        source=s,
        target=t,
        relation="r",
        weight=1.0,
        confidence="EXTRACTED",
        confidence_score=1.0,
    )


def _leiden_result(mapping: dict[str, int]) -> LeidenRunResult:
    return LeidenRunResult(
        job_type="full",
        node_to_community=mapping,
        levels=Q2_MAX_LEVEL,
        modularity=0.5,
        diff_percent=0.0,
        snapshot_path=Path("/tmp/test.pkl"),
    )


@pytest.fixture()
def llm() -> _FakeLLM:
    return _FakeLLM()


@pytest.fixture()
def sink() -> InMemorySummarySink:
    return InMemorySummarySink()


@pytest.fixture()
def svc(llm: _FakeLLM, sink: InMemorySummarySink) -> CommunitySummaryService:
    return CommunitySummaryService(
        llm=llm,
        sink=sink,
        prompt_template="level={level}\nnodes:\n{nodes}\nedges:\n{edges}",
        tenant_id="t1",
        leiden_job_id=42,
    )


# ============================================================
# build_memberships
# ============================================================


def test_build_memberships_drops_level_3(
    svc: CommunitySummaryService,
) -> None:
    """Q2: level 3 dropped. Service must stop at Q2_MAX_LEVEL=2."""
    nodes = [_node("A"), _node("B"), _node("C"), _node("D")]
    edges = [_edge("A", "B"), _edge("C", "D")]
    memberships = svc.build_memberships(
        leiden_result=_leiden_result({"A": 0, "B": 0, "C": 1, "D": 1}),
        nodes=nodes,
        edges=edges,
    )
    max_level = max(m.level for m in memberships)
    assert max_level <= Q2_MAX_LEVEL


def test_build_memberships_assigns_strategy(
    svc: CommunitySummaryService,
) -> None:
    """Q2: level 0 eager, deeper levels lazy."""
    memberships = svc.build_memberships(
        leiden_result=_leiden_result({"A": 0, "B": 0, "C": 1}),
        nodes=[_node("A"), _node("B"), _node("C")],
        edges=[],
    )
    for m in memberships:
        if m.level == 0:
            assert m.strategy == "eager"
        else:
            assert m.strategy == "lazy"


def test_build_memberships_filters_internal_edges(
    svc: CommunitySummaryService,
) -> None:
    """Only edges with both endpoints in the community are included."""
    memberships = svc.build_memberships(
        leiden_result=_leiden_result({"A": 0, "B": 0, "C": 1}),
        nodes=[_node("A"), _node("B"), _node("C")],
        edges=[_edge("A", "B"), _edge("A", "C")],  # only A-B is internal
    )
    comm_0 = next(m for m in memberships if m.community_id == 0 and m.level == 0)
    assert len(comm_0.edges) == 1
    assert comm_0.edges[0].source == "A"
    assert comm_0.edges[0].target == "B"


# ============================================================
# generate_eager
# ============================================================


@pytest.mark.asyncio
async def test_eager_generates_level_0_and_leaves(
    svc: CommunitySummaryService,
    sink: InMemorySummarySink,
) -> None:
    """Q2: eager covers level 0 AND every leaf (max-level) community."""
    memberships = svc.build_memberships(
        leiden_result=_leiden_result({"A": 0, "B": 0, "C": 1, "D": 1}),
        nodes=[_node("A"), _node("B"), _node("C"), _node("D")],
        edges=[_edge("A", "B"), _edge("C", "D")],
    )
    records = await svc.generate_eager(memberships)
    assert len(records) >= 2  # at least level 0 + leaf level
    # Every record is either level 0 or a leaf (Q2_MAX_LEVEL).
    for r in records:
        assert r.level == 0 or r.level == Q2_MAX_LEVEL
    # All records persisted.
    assert len(sink.records) == len(records)


@pytest.mark.asyncio
async def test_eager_skips_lazy_levels(
    svc: CommunitySummaryService,
    sink: InMemorySummarySink,
) -> None:
    """Q2: levels strictly between 0 and leaf are skipped in eager pass."""
    memberships = svc.build_memberships(
        leiden_result=_leiden_result({"A": 0, "B": 1, "C": 2, "D": 3}),
        nodes=[_node("A"), _node("B"), _node("C"), _node("D")],
        edges=[],
    )
    records = await svc.generate_eager(memberships)
    non_eager_levels = {r.level for r in records if r.level not in (0, Q2_MAX_LEVEL)}
    assert non_eager_levels == set()


# ============================================================
# get_or_generate (lazy)
# ============================================================


@pytest.mark.asyncio
async def test_lazy_generates_on_first_call(
    svc: CommunitySummaryService,
    sink: InMemorySummarySink,
    llm: _FakeLLM,
) -> None:
    memberships = svc.build_memberships(
        leiden_result=_leiden_result({"A": 0, "B": 0, "C": 1, "D": 1}),
        nodes=[_node("A"), _node("B"), _node("C"), _node("D")],
        edges=[],
    )
    # Pick any non-leaf, non-level-0 community.
    target = next(m for m in memberships if m.level not in (0, Q2_MAX_LEVEL))
    rec = await svc.get_or_generate(
        level=target.level,
        community_id=target.community_id,
        memberships=memberships,
    )
    assert rec.level == target.level
    assert rec.community_id == target.community_id
    assert (
        sink.fetch(
            leiden_job_id=42,
            level=target.level,
            community_id=target.community_id,
            tenant_id="t1",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_lazy_returns_cached_on_second_call(
    svc: CommunitySummaryService,
    llm: _FakeLLM,
) -> None:
    memberships = svc.build_memberships(
        leiden_result=_leiden_result({"A": 0, "B": 0, "C": 1}),
        nodes=[_node("A"), _node("B"), _node("C")],
        edges=[],
    )
    target = next(m for m in memberships if m.level not in (0, Q2_MAX_LEVEL))
    first = await svc.get_or_generate(
        level=target.level,
        community_id=target.community_id,
        memberships=memberships,
    )
    calls_before = len(llm.calls)
    second = await svc.get_or_generate(
        level=target.level,
        community_id=target.community_id,
        memberships=memberships,
    )
    assert second == first
    assert len(llm.calls) == calls_before  # cache hit


@pytest.mark.asyncio
async def test_lazy_raises_on_unknown_membership(
    svc: CommunitySummaryService,
) -> None:
    with pytest.raises(KeyError):
        await svc.get_or_generate(level=1, community_id=999, memberships=[])


@pytest.mark.asyncio
async def test_lazy_rejects_level_out_of_range(
    svc: CommunitySummaryService,
) -> None:
    with pytest.raises(ValueError):
        await svc.get_or_generate(level=3, community_id=0, memberships=[])


# ============================================================
# LLM output parsing
# ============================================================


def test_parse_llm_output_clean() -> None:
    title, summary = _parse_llm_output(
        "TITLE: New energy vehicles\nSUMMARY: The community focuses on EVs."
    )
    assert title == "New energy vehicles"
    assert summary == "The community focuses on EVs."


def test_parse_llm_output_truncates_long_title() -> None:
    long_title = "x" * 200
    title, _ = _parse_llm_output(f"TITLE: {long_title}\nSUMMARY: s")
    assert len(title) <= 80


def test_parse_llm_output_handles_no_markers() -> None:
    title, summary = _parse_llm_output("just some text without markers")
    assert title == "Untitled community"
    assert summary == "just some text without markers"


def test_parse_llm_output_handles_multiline_summary() -> None:
    title, summary = _parse_llm_output("TITLE: T\nSUMMARY: line1\nline2\nline3")
    assert title == "T"
    assert summary == "line1\nline2\nline3"


def test_parse_llm_output_handles_empty_summary() -> None:
    title, summary = _parse_llm_output("TITLE: T\nSUMMARY:")
    assert title == "T"
    assert summary == "(empty summary)"


# ============================================================
# Default prompt loader
# ============================================================


@pytest.mark.asyncio
async def test_load_default_prompt_returns_template() -> None:
    text = await load_default_prompt()
    assert "TITLE:" in text
    assert "SUMMARY:" in text


# ============================================================
# InMemorySink
# ============================================================


def test_in_memory_sink_round_trip() -> None:
    sink = InMemorySummarySink()
    rec = CommunitySummaryRecord(
        leiden_job_id=1,
        level=0,
        community_id=0,
        title="t",
        summary="s",
        member_count=2,
        member_node_ids=["A", "B"],
        generated_at=datetime.now(UTC),
        strategy="eager",
    )
    sink.write(rec, "t1")
    assert sink.fetch(leiden_job_id=1, level=0, community_id=0, tenant_id="t1") == rec
    assert sink.fetch(leiden_job_id=2, level=0, community_id=0, tenant_id="t1") is None
