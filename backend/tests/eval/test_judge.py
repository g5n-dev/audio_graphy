"""respx tests for LLMJudge — 4 cases per M5 arch §7.3.5.

Cases:
- extract_facts parses 3-line response
- judge_faithfulness parses 3-line JSONL
- judge_relevance parses single float
- malformed relevance response → 0.0 fallback with WARNING (caplog)
"""

from __future__ import annotations

import httpx
import pytest
import respx

from audio_graphy.adapters.real.llm_openai import LLMOpenAIAdapter
from audio_graphy.eval.judge import LLMJudge

_LLM_URL = "http://vllm-strong.test/v1/chat/completions"


def _openai_text(text: str) -> dict[str, object]:
    """Wrap a raw text response in the OpenAI chat completion schema."""
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": "qwen3.6-27b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def _make_judge() -> tuple[LLMJudge, LLMOpenAIAdapter]:
    """Return (judge, adapter) so the test can ``aclose`` the adapter cleanly."""
    adapter = LLMOpenAIAdapter(
        base_url="http://vllm-strong.test/v1",
        api_key="dummy-test-key",
        model="qwen3.6-27b",
    )
    return LLMJudge(llm=adapter), adapter


@pytest.mark.asyncio
async def test_extract_facts_parses_lines(respx_mock: respx.MockRouter) -> None:
    """3-line bulleted response → list len 3."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_text("- 事实1\n- 事实2\n- 事实3\n"),
        )
    )
    try:
        facts = await judge.extract_facts("dummy text")
        assert facts == ["事实1", "事实2", "事实3"]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_faithfulness_parses_jsonl(respx_mock: respx.MockRouter) -> None:
    """3-line JSONL response → [True, False, True]."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_text(
                '{"id": 1, "supported": true}\n'
                '{"id": 2, "supported": false}\n'
                '{"id": 3, "supported": true}\n'
            ),
        )
    )
    try:
        flags = await judge.judge_faithfulness("ctx", ["f1", "f2", "f3"])
        assert flags == [True, False, True]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_relevance_parses_float(respx_mock: respx.MockRouter) -> None:
    """Single-float response "1.0" → 1.0."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(200, json=_openai_text("1.0"))
    )
    try:
        score = await judge.judge_relevance("q", "a")
        assert score == pytest.approx(1.0)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_relevance_malformed_fallback(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-numeric response → 0.0 + WARNING log."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(200, json=_openai_text("无法判断"))
    )
    try:
        with caplog.at_level("WARNING", logger="audio_graphy.eval.judge"):
            score = await judge.judge_relevance("q", "a")
        assert score == pytest.approx(0.0)
        # WARNING log captured.
        assert any(
            "judge_relevance" in rec.message and "non-numeric" in rec.message
            for rec in caplog.records
        )
    finally:
        await adapter.aclose()


# ============================================================
# Extra coverage — push judge.py above 85% per arch §7.5.
# ============================================================


@pytest.mark.asyncio
async def test_judge_relevance_snaps_out_of_set(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """Numeric but out-of-set value (0.7) snaps to nearest allowed (0.5)."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(200, json=_openai_text("0.7"))
    )
    try:
        with caplog.at_level("WARNING", logger="audio_graphy.eval.judge"):
            score = await judge.judge_relevance("q", "a")
        assert score == pytest.approx(0.5)
        assert any("snapping" in rec.message for rec in caplog.records)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_relevance_wraps_backticks(
    respx_mock: respx.MockRouter
) -> None:
    """Relevance wrapped in markdown fences (`1.0`) → 1.0."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(200, json=_openai_text("`1.0`"))
    )
    try:
        score = await judge.judge_relevance("q", "a")
        assert score == pytest.approx(1.0)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_faithfulness_malformed_line(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """JSONL with one malformed line → that line defaults to False + WARNING."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_text(
                '{"id": 1, "supported": true}\n'
                "garbage-not-json\n"
                '{"id": 3, "supported": false}\n'
            ),
        )
    )
    try:
        with caplog.at_level("WARNING", logger="audio_graphy.eval.judge"):
            flags = await judge.judge_faithfulness("ctx", ["f1", "f2", "f3"])
        assert flags == [True, False, False]
        assert any("malformed line" in rec.message for rec in caplog.records)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_faithfulness_pad_when_llm_under_returns(
    respx_mock: respx.MockRouter
) -> None:
    """LLM returns fewer verdicts than facts → pad with False + WARNING."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_text('{"id": 1, "supported": true}\n'),
        )
    )
    try:
        flags = await judge.judge_faithfulness("ctx", ["f1", "f2", "f3"])
        assert flags == [True, False, False]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_faithfulness_truncate_when_over_returns(
    respx_mock: respx.MockRouter
) -> None:
    """LLM returns more verdicts than facts → truncate to expected count."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_text(
                '{"id": 1, "supported": true}\n'
                '{"id": 2, "supported": true}\n'
                '{"id": 3, "supported": true}\n'
            ),
        )
    )
    try:
        flags = await judge.judge_faithfulness("ctx", ["f1"])
        assert flags == [True]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_faithfulness_empty_facts_short_circuits(
    respx_mock: respx.MockRouter
) -> None:
    """Empty facts list → no LLM call, returns []."""
    judge, adapter = _make_judge()
    try:
        flags = await judge.judge_faithfulness("ctx", [])
        assert flags == []
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_extract_facts_no_lines(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """LLM returns empty / non-bulleted gibberish → [] + WARNING."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(200, json=_openai_text(""))
    )
    try:
        with caplog.at_level("WARNING", logger="audio_graphy.eval.judge"):
            facts = await judge.extract_facts("dummy")
        assert facts == []
        assert any("no parseable" in rec.message for rec in caplog.records)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_extract_facts_strips_numbered_prefix(
    respx_mock: respx.MockRouter
) -> None:
    """Lines like ``1. fact`` are normalized to bare text."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_text("1. first\n2. second\n* third\n"),
        )
    )
    try:
        facts = await judge.extract_facts("dummy")
        assert facts == ["first", "second", "third"]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_judge_aclose_forwards_to_llm() -> None:
    """aclose() forwards to underlying adapter when it has the method."""
    adapter = LLMOpenAIAdapter(
        base_url="http://vllm-strong.test/v1",
        api_key="dummy",
        model="qwen3.6-27b",
    )
    judge = LLMJudge(llm=adapter)
    # Just verify it doesn't raise; the adapter's own aclose is tested elsewhere.
    await judge.aclose()


@pytest.mark.asyncio
async def test_judge_relevance_markdown_asterisk(
    respx_mock: respx.MockRouter
) -> None:
    """``**1.0**`` (markdown bold) → 1.0."""
    judge, adapter = _make_judge()
    respx_mock.post(_LLM_URL).mock(
        return_value=httpx.Response(200, json=_openai_text("**1.0**"))
    )
    try:
        score = await judge.judge_relevance("q", "a")
        assert score == pytest.approx(1.0)
    finally:
        await adapter.aclose()
