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

import audio_graphy.core.community_summary as community_summary_module
from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.core.community_summary import (
    Q2_MAX_LEVEL,
    CommunityMembership,
    CommunitySummaryRecord,
    CommunitySummaryService,
    InMemorySummarySink,
    _parse_llm_output,
    load_default_prompt,
)
from audio_graphy.core.leiden import LeidenRunResult
from audio_graphy.core.types import GraphEdge, GraphNode
from audio_graphy.services.llm_gateway import LLMRequest

# ============================================================
# Fakes
# ============================================================


class _FakeLLM:
    """Returns a deterministic TITLE/SUMMARY pair."""

    model = "fake-community-summary"
    provider = "test-provider"
    model_epoch = "fake-epoch-1"

    def __init__(self, title: str = "Test Title", summary: str = "Test Summary") -> None:
        self.title = title
        self.summary = summary
        self.calls: list[str] = []

    async def execute(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(str(request.messages[-1]["content"]))
        return LLMResponse(
            text=f"TITLE: {self.title}\nSUMMARY: {self.summary}",
            model=self.model,
            prompt_hash=request.recipe_sha256(model=self.model),
        )


def _node(
    eid: str,
    t: str = "车型",
    *,
    recording_ids: list[int] | None = None,
) -> GraphNode:
    return GraphNode(
        entity_id=eid,
        name=eid,
        type=t,
        description=f"desc_{eid}",
        source_ids=[],
        recording_ids=[1] if recording_ids is None else recording_ids,
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


@pytest.mark.asyncio
async def test_eager_returns_persisted_summary_without_calling_llm(
    svc: CommunitySummaryService,
    llm: _FakeLLM,
    sink: InMemorySummarySink,
) -> None:
    persisted = CommunitySummaryRecord(
        leiden_job_id=42,
        level=0,
        community_id=7,
        title="Persisted",
        summary="Already generated by an earlier job.",
        member_count=1,
        member_node_ids=["A"],
        generated_at=datetime.now(UTC),
        strategy="eager",
    )
    sink.write(persisted, "t1")
    membership = CommunityMembership(
        level=0,
        community_id=7,
        nodes=[_node("A")],
        edges=[],
        strategy="eager",
    )

    records = await svc.generate_eager([membership])

    assert records == [persisted]
    assert llm.calls == []
    assert sink.records == [persisted]


@pytest.mark.asyncio
async def test_summary_request_uses_complete_tenant_scoped_gateway_recipe(
    monkeypatch: pytest.MonkeyPatch,
    llm: _FakeLLM,
    sink: InMemorySummarySink,
) -> None:
    captured: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        captured.append(request)
        return LLMResponse(
            text="TITLE: 车型与金融\nSUMMARY: 车型 A 与金融方案 B 存在关联。",
            model=llm.model,
            prompt_hash=request.recipe_sha256(model=llm.model),
        )

    monkeypatch.setattr(community_summary_module, "execute_llm", capture)
    service = CommunitySummaryService(
        llm=llm,  # type: ignore[arg-type]
        sink=sink,
        prompt_template="Stable community summary system instruction.",
        tenant_id="tenant-a",
        leiden_job_id=42,
    )
    membership = CommunityMembership(
        level=0,
        community_id=7,
        nodes=[
            _node("A", recording_ids=[9, 1]),
            _node("B", recording_ids=[9, 2]),
        ],
        edges=[_edge("A", "A")],
        strategy="eager",
    )

    [record] = await service.generate_eager([membership])

    assert record.title == "车型与金融"
    [request] = captured
    assert request.tenant_id == "tenant-a"
    assert request.purpose == "community_summary"
    assert request.model_tier == "weak"
    assert request.provider == "test-provider"
    assert request.model_epoch == "fake-epoch-1"
    assert request.temperature == 0
    assert request.top_p == 1
    assert request.max_tokens == 256
    assert request.ttl_seconds == 90 * 24 * 60 * 60
    assert request.prompt_version.startswith("community-summary-prompt-v1:")
    assert request.schema_version == "community-summary-schema-v1"
    assert request.parser_version == "community-summary-parser-v1"
    assert request.postprocessor_version == "community-summary-postprocessor-v1"
    assert request.response_schema is not None
    assert len(str(request.business_snapshot["content_sha256"])) == 64
    assert request.permission_scope == {
        "tenant_id": "tenant-a",
        "leiden_job_id": 42,
        "community_id": 7,
        "level": 0,
    }
    assert {
        (item.source_type, item.source_id)  # type: ignore[union-attr]
        for item in request.provenance
    } == {
        ("community", "42:0:7"),
        ("leiden_job", "42"),
        ("recording", "1"),
        ("recording", "2"),
        ("recording", "9"),
    }
    assert [message["role"] for message in request.messages] == ["system", "user"]
    assert request.messages[0]["content"] == "Stable community summary system instruction."
    assert "LEVEL: 0" in str(request.messages[-1]["content"])
    assert "A (车型): desc_A" in str(request.messages[-1]["content"])
    assert request.response_validator is not None
    assert request.response_validator(LLMResponse("TITLE: T\nSUMMARY: S", llm.model, "a" * 64))
    assert not request.response_validator(LLMResponse("unstructured", llm.model, "b" * 64))


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
