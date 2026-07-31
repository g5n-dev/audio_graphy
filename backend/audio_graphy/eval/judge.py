"""LLMJudge — wraps an LLMOpenAIAdapter to run 3 evaluation judge prompts.

LLM-as-judge 实现：复用 ``LLMOpenAIAdapter``（strong），3 个 prompt 模板：
- extract_facts: 抽取原子事实列表
- judge_faithfulness: 逐条事实是否被上下文支持
- judge_relevance: 回答相关性评分（0/0.5/1）

Design:
- Lazy prompt loading via importlib.resources (works after pip install).
- Rich tenant-scoped LLMRequest recipes with optional dataset/example refs.
- All parse failures log WARNING and return safe defaults (no exception).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from audio_graphy.adapters.protocols import LLMAdapter, LLMResponse
from audio_graphy.services.llm_gateway import (
    CachePolicy,
    LLMProvenance,
    LLMRequest,
    execute_llm,
)

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT_DIR = Path(__file__).parent / "prompts"
_ALLOWED_RELEVANCE = (0.0, 0.5, 1.0)
_EVAL_TTL_SECONDS = 90 * 24 * 60 * 60
_EVAL_POSTPROCESSOR_VERSION = "eval-safe-defaults-v1"


class LLMJudge:
    """LLM-as-judge wrapping an LLMOpenAIAdapter for the 3 eval prompts.

    用法 / Usage::

        judge = LLMJudge(llm=strong_llm)
        facts = await judge.extract_facts("今天我们讨论了 CS75 Plus 的价格。")
        flags = await judge.judge_faithfulness(context_text, facts)
        score = await judge.judge_relevance("优惠多少？", "5 万元现金优惠。")

    Args:
        llm: Any gateway or compatible transport adapter returning
            ``LLMResponse``.
        prompt_dir: Directory containing the 3 prompt templates. Defaults to
            ``audio_graphy/eval/prompts/`` next to this module.
        tenant_id: Default tenant scope; ``"default"`` preserves legacy callers.
        dataset_id: Optional evaluation dataset provenance shared by calls.
        permission_scope: Optional authorization snapshot for recipe isolation.
    """

    def __init__(
        self,
        llm: LLMAdapter,
        *,
        prompt_dir: Path | None = None,
        tenant_id: str = "default",
        dataset_id: str | int | None = None,
        permission_scope: Mapping[str, Any] | None = None,
    ) -> None:
        self._llm = llm
        self._prompt_dir = prompt_dir or _DEFAULT_PROMPT_DIR
        self._prompt_cache: dict[str, str] = {}
        self._tenant_id = tenant_id
        self._dataset_id = dataset_id
        self._permission_scope = (
            dict(permission_scope) if permission_scope else {"tenant_id": tenant_id}
        )

    # --------------------------------------------------------------
    # Public methods
    # --------------------------------------------------------------
    async def extract_facts(
        self,
        text: str,
        *,
        tenant_id: str | None = None,
        dataset_id: str | int | None = None,
        example_id: str | int | None = None,
        permission_scope: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Extract atomic facts from ``text``.

        Returns ``[]`` on empty / unparseable response (with WARNING).
        """
        template = self._load_prompt("extract_facts.txt")
        prompt = template.format(text=text)
        resp_text = await self._call_llm(
            prompt,
            purpose="extract_facts",
            prompt_template=template,
            schema_version="eval-fact-list-v1",
            parser_version="eval-fact-list-parser-v1",
            response_schema={
                "type": "string",
                "format": "one-bulleted-atomic-fact-per-line",
            },
            business_snapshot={
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            response_validator=self._valid_fact_response,
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            example_id=example_id,
            permission_scope=permission_scope,
        )
        return self._parse_fact_list(resp_text)

    async def judge_faithfulness(
        self,
        context: str,
        facts: list[str],
        *,
        tenant_id: str | None = None,
        dataset_id: str | int | None = None,
        example_id: str | int | None = None,
        permission_scope: Mapping[str, Any] | None = None,
    ) -> list[bool]:
        """Judge each fact against ``context``.

        Returns a list of bools aligned to ``facts`` (pad/truncate to len).
        Malformed lines default to ``False`` + WARNING.
        """
        if not facts:
            return []
        numbered = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(facts))
        template = self._load_prompt("judge_faithfulness.txt")
        prompt = template.format(context=context, numbered_facts=numbered)
        resp_text = await self._call_llm(
            prompt,
            purpose="judge_faithfulness",
            prompt_template=template,
            schema_version="eval-faithfulness-jsonl-v1",
            parser_version="eval-faithfulness-jsonl-parser-v1",
            response_schema={
                "type": "string",
                "format": "jsonl",
                "expected_count": len(facts),
            },
            business_snapshot={
                "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
                "facts_sha256": hashlib.sha256("\0".join(facts).encode("utf-8")).hexdigest(),
                "fact_count": len(facts),
            },
            response_validator=lambda response: self._valid_faithfulness_response(
                response,
                expected_count=len(facts),
            ),
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            example_id=example_id,
            permission_scope=permission_scope,
        )
        return self._parse_jsonl_verdicts(resp_text, expected_count=len(facts))

    async def judge_relevance(
        self,
        query: str,
        answer: str,
        *,
        tenant_id: str | None = None,
        dataset_id: str | int | None = None,
        example_id: str | int | None = None,
        permission_scope: Mapping[str, Any] | None = None,
    ) -> float:
        """Score relevance ∈ {0.0, 0.5, 1.0}.

        Out-of-set numeric values are snapped to the nearest allowed value
        with WARNING. Non-numeric responses default to ``0.0`` with WARNING.
        """
        template = self._load_prompt("judge_relevance.txt")
        prompt = template.format(query=query, answer=answer)
        resp_text = await self._call_llm(
            prompt,
            purpose="judge_relevance",
            prompt_template=template,
            schema_version="eval-relevance-score-v1",
            parser_version="eval-relevance-score-parser-v1",
            response_schema={
                "type": "number",
                "enum": list(_ALLOWED_RELEVANCE),
            },
            business_snapshot={
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            },
            response_validator=self._valid_relevance_response,
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            example_id=example_id,
            permission_scope=permission_scope,
        )
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
    # Helpers — centralized LLM call
    # --------------------------------------------------------------
    async def _call_llm(
        self,
        prompt: str,
        *,
        purpose: str,
        prompt_template: str,
        schema_version: str,
        parser_version: str,
        response_schema: Mapping[str, Any],
        business_snapshot: Mapping[str, Any],
        response_validator: Callable[[LLMResponse], bool],
        tenant_id: str | None,
        dataset_id: str | int | None,
        example_id: str | int | None,
        permission_scope: Mapping[str, Any] | None,
    ) -> str:
        messages = [{"role": "user", "content": prompt}]
        resolved_tenant = tenant_id or self._tenant_id
        resolved_dataset = dataset_id if dataset_id is not None else self._dataset_id
        resolved_scope = (
            dict(permission_scope)
            if permission_scope
            else (
                dict(self._permission_scope)
                if resolved_tenant == self._tenant_id
                else {"tenant_id": resolved_tenant}
            )
        )
        provenance: list[LLMProvenance] = []
        if resolved_dataset is not None:
            provenance.append(LLMProvenance("eval_dataset", str(resolved_dataset)))
        if example_id is not None:
            provenance.append(LLMProvenance("eval_example", str(example_id)))
        adapter = self._llm
        request = LLMRequest(
            tenant_id=resolved_tenant,
            purpose=purpose,
            model_tier="strong",
            provider=str(getattr(adapter, "provider", "openai-compatible")),
            model_epoch=str(getattr(adapter, "model_epoch", adapter.model)),
            messages=messages,
            prompt_version=(
                f"{purpose}:{hashlib.sha256(prompt_template.encode('utf-8')).hexdigest()}"
            ),
            schema_version=schema_version,
            parser_version=parser_version,
            postprocessor_version=_EVAL_POSTPROCESSOR_VERSION,
            temperature=0.0,
            top_p=1.0,
            response_schema=response_schema,
            business_snapshot=dict(business_snapshot),
            permission_scope=resolved_scope,
            provenance=tuple(provenance),
            cache_policy=CachePolicy.EXACT,
            ttl_seconds=_EVAL_TTL_SECONDS,
            response_validator=response_validator,
        )
        response = await execute_llm(adapter, request)
        return response.text

    @staticmethod
    def _valid_fact_response(response: LLMResponse) -> bool:
        """Accept legal empty output or a strict bullet/numbered fact list."""

        lines = [line.strip() for line in response.text.splitlines() if line.strip()]
        if not lines:
            return True
        return all(re.match(r"^(?:[-*]\s+|\d+\.\s+)\S", line) is not None for line in lines)

    @staticmethod
    def _valid_faithfulness_response(
        response: LLMResponse,
        *,
        expected_count: int,
    ) -> bool:
        """Require exactly one ordered boolean JSON verdict per fact."""

        lines = [line.strip() for line in response.text.splitlines() if line.strip()]
        if len(lines) != expected_count:
            return False
        for expected_id, line in enumerate(lines, 1):
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                return False
            if (
                not isinstance(item, dict)
                or set(item) != {"id", "supported"}
                or item["id"] != expected_id
                or not isinstance(item["supported"], bool)
            ):
                return False
        return True

    @staticmethod
    def _valid_relevance_response(response: LLMResponse) -> bool:
        """Cache only an exact allowed relevance score."""

        cleaned = response.text.strip().strip("*`").strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", cleaned) is None:
            return False
        try:
            return float(cleaned) in _ALLOWED_RELEVANCE
        except ValueError:
            return False

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
