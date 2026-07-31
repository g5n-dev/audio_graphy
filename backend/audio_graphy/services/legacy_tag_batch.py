"""One-call compatibility tagger for the three legacy recording tag paths.

The legacy endpoints historically issued one LLM call per tag path and used a
cache key that omitted the transcript.  This service sends every requested
path in one structured request and accepts only a complete, schema-valid
response.  Persistence and content-addressed caching are supplied by
``LLMGateway`` when the configured adapter is gateway-backed.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from audio_graphy.adapters.protocols import LLMAdapter, LLMResponse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MAX_TRANSCRIPT_CHARS = 20_000
_MAX_TAG_PATHS = 128
_TAG_TTL_SECONDS = 90 * 24 * 60 * 60
_ALLOWED_VALUES = frozenset({"pass", "fail"})


class LegacyTagBatchError(ValueError):
    """The structured legacy-tag response is incomplete or invalid."""


@dataclass(frozen=True, slots=True)
class LegacyTagBatchResult:
    """Validated values returned by one batched LLM call."""

    values: dict[str, str]
    confidences: dict[str, float]
    prompt_hash: str
    cached: bool
    cache_source: str
    provider_calls: int
    estimated_input_tokens: int
    provider_input_tokens: int
    provider_output_tokens: int


class LegacyTagBatcher:
    """Classify multiple legacy tag paths with exactly one LLM request."""

    def __init__(self, llm: LLMAdapter) -> None:
        self._llm = llm

    @staticmethod
    def _response_schema(paths: tuple[str, ...]) -> dict[str, object]:
        """Bind every requested path to its own strict result branch."""

        branches = [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["tag_path", "value", "confidence"],
                "properties": {
                    "tag_path": {"const": path},
                    "value": {"type": "string", "enum": sorted(_ALLOWED_VALUES)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            }
            for path in paths
        ]
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["tags"],
            "properties": {
                "tags": {
                    "type": "array",
                    "minItems": len(paths),
                    "maxItems": len(paths),
                    "items": {"anyOf": branches},
                }
            },
        }

    @staticmethod
    def _output_token_budget(tag_count: int) -> int:
        requested = 128 + (96 * tag_count)
        return min(2_048, max(256, math.ceil(requested / 256) * 256))

    async def classify(
        self,
        *,
        tenant_id: str,
        recording_id: int,
        transcript: str,
        tag_paths: Sequence[str],
        prompt_version: str,
        prompt_content: str | None = None,
    ) -> LegacyTagBatchResult:
        paths = tuple(str(path).strip() for path in tag_paths)
        if not tenant_id:
            raise LegacyTagBatchError("tenant_id is required")
        if recording_id <= 0:
            raise LegacyTagBatchError("recording_id must be positive")
        if not paths or len(paths) > _MAX_TAG_PATHS:
            raise LegacyTagBatchError(f"tag_paths must contain 1..{_MAX_TAG_PATHS} items")
        if any(not path for path in paths) or len(set(paths)) != len(paths):
            raise LegacyTagBatchError("tag_paths must be non-empty and unique")

        response_schema = self._response_schema(paths)
        request_payload = {
            "k": list(paths),
            "t": transcript[:_MAX_TRANSCRIPT_CHARS],
        }
        system_content = (
            "你是门店接待质检分类器。依据标签规则判断对话；"
            "k 是待判定标签，t 是转写。每个 k 恰好返回一次，禁止新增标签。"
        )
        if prompt_content is not None and prompt_content.strip():
            system_content += "\n标签规则：\n" + prompt_content.strip()
        messages = [
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]
        response = await self._complete(
            messages=messages,
            tenant_id=tenant_id,
            prompt_version=prompt_version,
            recording_id=recording_id,
            transcript=transcript,
            tag_paths=paths,
            response_schema=response_schema,
        )
        values, confidences = self._parse(response.text, paths)
        cache_source = str(getattr(response, "cache_source", "provider"))
        provider_called = bool(getattr(response, "provider_called", not response.cached))
        provider_input_tokens = _usage_value(response.usage, "prompt_tokens", "input_tokens")
        provider_output_tokens = _usage_value(
            response.usage,
            "completion_tokens",
            "output_tokens",
        )
        return LegacyTagBatchResult(
            values=values,
            confidences=confidences,
            prompt_hash=response.prompt_hash,
            cached=response.cached,
            cache_source=cache_source,
            provider_calls=int(provider_called),
            estimated_input_tokens=_estimate_message_tokens(messages),
            provider_input_tokens=(provider_input_tokens if provider_called else 0),
            provider_output_tokens=(provider_output_tokens if provider_called else 0),
        )

    async def _complete(
        self,
        *,
        messages: list[dict[str, str]],
        tenant_id: str,
        prompt_version: str,
        recording_id: int,
        transcript: str,
        tag_paths: tuple[str, ...],
        response_schema: Mapping[str, Any],
    ) -> LLMResponse:
        # The canonical gateway contract is mandatory even when the injected
        # transport is a legacy-compatible test adapter.
        from audio_graphy.services.llm_gateway import (
            CachePolicy,
            LLMProvenance,
            LLMRequest,
            execute_llm,
        )

        def _validate_response(response: LLMResponse) -> bool:
            self._parse(response.text, tag_paths)
            return True

        request = LLMRequest(
            tenant_id=tenant_id,
            purpose="legacy_tag_batch",
            model_tier="weak",
            provider=str(getattr(self._llm, "provider", "openai-compatible")),
            model_epoch=str(getattr(self._llm, "model_epoch", self._llm.model)),
            messages=tuple(messages),
            prompt_version=prompt_version,
            schema_version="legacy-tag-batch-schema-v1",
            parser_version="legacy-tag-batch-v1",
            postprocessor_version="legacy-tag-batch-normalize-v1",
            business_snapshot={
                "recording_id": recording_id,
                "transcript": transcript[:_MAX_TRANSCRIPT_CHARS],
                "tag_paths": list(tag_paths),
            },
            permission_scope={"tenant_id": tenant_id},
            provenance=(LLMProvenance(source_type="recording", source_id=str(recording_id)),),
            cache_policy=CachePolicy.EXACT,
            temperature=0.0,
            top_p=1.0,
            max_tokens=self._output_token_budget(len(tag_paths)),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "legacy_tag_batch_v2",
                    "description": "One strict classification per requested tag path",
                },
            },
            response_schema=response_schema,
            ttl_seconds=_TAG_TTL_SECONDS,
            response_validator=_validate_response,
        )
        return await execute_llm(self._llm, request)

    @staticmethod
    def _parse(
        raw_text: str,
        requested_paths: tuple[str, ...],
    ) -> tuple[dict[str, str], dict[str, float]]:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise LegacyTagBatchError("LLM returned invalid JSON") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("tags"), list):
            raise LegacyTagBatchError("LLM JSON must contain tags[]")

        rows = payload["tags"]
        parsed: dict[str, tuple[str, float]] = {}
        duplicates: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise LegacyTagBatchError("each tags[] item must be an object")
            tag_path = str(row.get("tag_path", "")).strip()
            if tag_path in parsed:
                duplicates.add(tag_path)
                continue
            if tag_path not in requested_paths:
                raise LegacyTagBatchError(f"unexpected tag_path: {tag_path}")
            value = str(row.get("value", "")).strip().lower()
            if value not in _ALLOWED_VALUES:
                raise LegacyTagBatchError(f"invalid value for {tag_path}")
            raw_confidence = row.get("confidence")
            if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, int | float):
                raise LegacyTagBatchError(f"invalid confidence for {tag_path}")
            confidence = float(raw_confidence)
            if not 0.0 <= confidence <= 1.0:
                raise LegacyTagBatchError(f"invalid confidence for {tag_path}")
            parsed[tag_path] = (value, confidence)

        if duplicates or set(parsed) != set(requested_paths) or len(parsed) != len(requested_paths):
            raise LegacyTagBatchError("response must contain exactly one result per requested tag")
        return (
            {path: parsed[path][0] for path in requested_paths},
            {path: parsed[path][1] for path in requested_paths},
        )


def _usage_value(usage: Mapping[str, int], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


@lru_cache(maxsize=1)
def _token_encoding() -> object | None:
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _estimate_message_tokens(messages: Sequence[Mapping[str, str]]) -> int:
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    encoding = _token_encoding()
    if encoding is not None:
        encode = getattr(encoding, "encode", None)
        if callable(encode):
            return len(encode(serialized))
    return max(1, math.ceil(len(serialized.encode("utf-8")) / 4))


async def load_recording_transcript(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    recording_id: int,
) -> str:
    """Load tenant-scoped, scrubbed segment text in deterministic order."""
    from sqlalchemy import select

    from audio_graphy.core.pii import scrubbed_segment_text
    from audio_graphy.models.segment import Segment

    async with session_factory() as session:
        segments = list(
            (
                await session.execute(
                    select(Segment)
                    .where(
                        Segment.tenant_id == tenant_id,
                        Segment.recording_id == recording_id,
                    )
                    .order_by(Segment.idx, Segment.id)
                )
            )
            .scalars()
            .all()
        )
    return "\n".join(
        text
        for segment in segments
        if (text := scrubbed_segment_text(segment.text_scrubbed, segment.transcript))
    )


__all__ = [
    "LegacyTagBatchError",
    "LegacyTagBatchResult",
    "LegacyTagBatcher",
    "load_recording_transcript",
]
