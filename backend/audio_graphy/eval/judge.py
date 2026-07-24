"""LLMJudge — wraps an LLMOpenAIAdapter to run 3 evaluation judge prompts.

LLM-as-judge 实现：复用 ``LLMOpenAIAdapter``（strong），3 个 prompt 模板：
- extract_facts: 抽取原子事实列表
- judge_faithfulness: 逐条事实是否被上下文支持
- judge_relevance: 回答相关性评分（0/0.5/1）

Design:
- Lazy prompt loading via importlib.resources (works after pip install).
- MD5 cache_key per call — same (method, text) reuses LLMOpenAIAdapter cache.
- All parse failures log WARNING and return safe defaults (no exception).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Protocol

from audio_graphy.adapters.protocols import LLMAdapter

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT_DIR = Path(__file__).parent / "prompts"
_ALLOWED_RELEVANCE = (0.0, 0.5, 1.0)


class _LLMAdapterProto(Protocol):
    """Structural superset of LLMOpenAIAdapter used by LLMJudge."""

    model: str

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
    ) -> object: ...


class LLMJudge:
    """LLM-as-judge wrapping an LLMOpenAIAdapter for the 3 eval prompts.

    用法 / Usage::

        judge = LLMJudge(llm=strong_llm)
        facts = await judge.extract_facts("今天我们讨论了 CS75 Plus 的价格。")
        flags = await judge.judge_faithfulness(context_text, facts)
        score = await judge.judge_relevance("优惠多少？", "5 万元现金优惠。")

    Args:
        llm: Any ``LLMOpenAIAdapter``-compatible adapter (must accept
            ``cache_key=`` and return ``LLMResponse`` with ``.text``).
        prompt_dir: Directory containing the 3 prompt templates. Defaults to
            ``audio_graphy/eval/prompts/`` next to this module.
    """

    def __init__(
        self,
        llm: LLMAdapter,
        *,
        prompt_dir: Path | None = None,
    ) -> None:
        self._llm = llm
        self._prompt_dir = prompt_dir or _DEFAULT_PROMPT_DIR
        self._prompt_cache: dict[str, str] = {}

    # --------------------------------------------------------------
    # Public methods
    # --------------------------------------------------------------
    async def extract_facts(self, text: str) -> list[str]:
        """Extract atomic facts from ``text``.

        Returns ``[]`` on empty / unparseable response (with WARNING).
        """
        cache_key = self._cache_key("extract_facts", text)
        prompt = self._load_prompt("extract_facts.txt").format(text=text)
        resp_text = await self._call_llm(prompt, cache_key=cache_key)
        return self._parse_fact_list(resp_text)

    async def judge_faithfulness(self, context: str, facts: list[str]) -> list[bool]:
        """Judge each fact against ``context``.

        Returns a list of bools aligned to ``facts`` (pad/truncate to len).
        Malformed lines default to ``False`` + WARNING.
        """
        if not facts:
            return []
        numbered = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(facts))
        cache_key = self._cache_key("faith", context, "\n".join(facts))
        prompt = self._load_prompt("judge_faithfulness.txt").format(
            context=context, numbered_facts=numbered
        )
        resp_text = await self._call_llm(prompt, cache_key=cache_key)
        return self._parse_jsonl_verdicts(resp_text, expected_count=len(facts))

    async def judge_relevance(self, query: str, answer: str) -> float:
        """Score relevance ∈ {0.0, 0.5, 1.0}.

        Out-of-set numeric values are snapped to the nearest allowed value
        with WARNING. Non-numeric responses default to ``0.0`` with WARNING.
        """
        cache_key = self._cache_key("rel", query, answer)
        prompt = self._load_prompt("judge_relevance.txt").format(query=query, answer=answer)
        resp_text = await self._call_llm(prompt, cache_key=cache_key)
        return self._parse_relevance_score(resp_text)

    async def aclose(self) -> None:
        """Forward close to the underlying LLM adapter (if supported)."""
        aclose = getattr(self._llm, "aclose", None)
        if callable(aclose):
            await aclose()

    # --------------------------------------------------------------
    # Helpers — prompt loading
    # --------------------------------------------------------------
    def _load_prompt(self, filename: str) -> str:
        cached = self._prompt_cache.get(filename)
        if cached is not None:
            return cached
        path = self._prompt_dir / filename
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("prompt template missing: %s (%s)", path, exc)
            raise
        # ``str.format`` will interpret ``{text}`` etc.; ensure no stray braces
        # in templates other than the expected placeholders.
        self._prompt_cache[filename] = text
        return text

    # --------------------------------------------------------------
    # Helpers — LLM call + cache_key
    # --------------------------------------------------------------
    def _cache_key(self, *parts: str) -> str:
        joined = "|".join(str(p) for p in parts)
        return hashlib.md5(joined.encode("utf-8")).hexdigest()

    async def _call_llm(self, prompt: str, *, cache_key: str) -> str:
        messages = [{"role": "user", "content": prompt}]
        resp = await self._llm.complete(
            messages,
            temperature=0.0,
            cache_key=cache_key,
        )
        # LLMResponse.text — duck-typed to keep this module protocol-shaped.
        return str(getattr(resp, "text", ""))

    # --------------------------------------------------------------
    # Helpers — response parsing
    # --------------------------------------------------------------
    @staticmethod
    def _parse_fact_list(text: str) -> list[str]:
        """Parse ``"- fact1\\n- fact2\\n..."`` into a clean list.

        Strips leading ``-`` / ``1.`` / ``*`` markers and trims each line.
        Empty lines are skipped. Returns ``[]`` if no facts found.
        """
        facts: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            line = re.sub(r"^\s*[-*]\s+", "", line)
            line = re.sub(r"^\s*\d+\.\s+", "", line)
            line = line.strip()
            if line:
                facts.append(line)
        if not facts:
            logger.warning("extract_facts returned no parseable facts: %r", text[:120])
        return facts

    @staticmethod
    def _parse_jsonl_verdicts(text: str, *, expected_count: int) -> list[bool]:
        """Parse JSON-per-line ``{"id": int, "supported": bool}`` into bool list.

        Malformed lines default to ``False`` + WARNING. Result is padded /
        truncated to ``expected_count`` (per id alignment is not enforced —
        we treat the LLM as emitting 1 verdict per line in input order).
        """
        out: list[bool] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip markdown fences if the LLM wrapped each line in ``` ``` .
            line = line.strip("`")
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("judge_faithfulness malformed line: %r", line[:80])
                out.append(False)
                continue
            supported = obj.get("supported", False) if isinstance(obj, dict) else False
            out.append(bool(supported))
        # Pad / truncate to expected_count.
        if len(out) < expected_count:
            logger.warning(
                "judge_faithfulness returned %d verdicts, expected %d — padding False",
                len(out),
                expected_count,
            )
            out.extend([False] * (expected_count - len(out)))
        elif len(out) > expected_count:
            logger.warning(
                "judge_faithfulness returned %d verdicts, expected %d — truncating",
                len(out),
                expected_count,
            )
            out = out[:expected_count]
        return out

    @staticmethod
    def _parse_relevance_score(text: str) -> float:
        """Parse a single float ∈ {0.0, 0.5, 1.0}.

        Strips markdown decorations (``*``, `` ` ``). Non-numeric or
        out-of-set values snap to the nearest allowed value with WARNING.
        """
        cleaned = text.strip().strip("*`").strip()
        # Take the first numeric token.
        match = re.search(r"\d+(?:\.\d+)?", cleaned)
        if not match:
            logger.warning("judge_relevance non-numeric response: %r", text[:80])
            return 0.0
        try:
            value = float(match.group(0))
        except ValueError:
            logger.warning("judge_relevance unparseable numeric: %r", text[:80])
            return 0.0
        # Snap to nearest allowed value.
        nearest = min(_ALLOWED_RELEVANCE, key=lambda v: abs(v - value))
        if nearest != value:
            logger.warning(
                "judge_relevance %g not in {0, 0.5, 1} — snapping to %g",
                value,
                nearest,
            )
        return nearest


__all__ = ["LLMJudge"]
