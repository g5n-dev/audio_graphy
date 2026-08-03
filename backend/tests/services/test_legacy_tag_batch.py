from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.services.legacy_tag_batch import (
    LegacyTagBatcher,
    LegacyTagBatchError,
)


class _StructuredTagLLM:
    model = "weak-test"

    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.calls: list[Sequence[dict[str, str]]] = []
        self.generation_kwargs: list[dict[str, object]] = []

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        **generation_kwargs: object,
    ) -> LLMResponse:
        del temperature, cache_key
        self.calls.append(messages)
        self.generation_kwargs.append({"max_tokens": max_tokens, **generation_kwargs})
        return LLMResponse(
            text=json.dumps(self._payload, ensure_ascii=False),
            model=self.model,
            prompt_hash="provider-hash",
        )


@pytest.mark.asyncio
async def test_three_legacy_tags_are_classified_in_one_llm_call() -> None:
    llm = _StructuredTagLLM(
        {
            "tags": [
                {"tag_path": "quality.greeting", "value": "pass", "confidence": 0.98},
                {"tag_path": "quality.closing", "value": "fail", "confidence": 0.91},
                {"tag_path": "sales.product_mention", "value": "pass", "confidence": 0.95},
            ]
        }
    )
    batcher = LegacyTagBatcher(llm)

    result = await batcher.classify(
        tenant_id="tenant-a",
        recording_id=42,
        transcript="坐席：您好。客户：我想了解 CS75 Plus。",
        tag_paths=(
            "quality.greeting",
            "quality.closing",
            "sales.product_mention",
        ),
        prompt_version="tag_prompt_v2",
    )

    assert len(llm.calls) == 1
    assert result.values == {
        "quality.greeting": "pass",
        "quality.closing": "fail",
        "sales.product_mention": "pass",
    }
    assert result.confidences["quality.greeting"] == pytest.approx(0.98)
    request_payload = json.loads(llm.calls[0][1]["content"])
    assert request_payload["k"] == [
        "quality.greeting",
        "quality.closing",
        "sales.product_mention",
    ]
    assert request_payload["t"].startswith("坐席：您好")
    assert set(request_payload) == {"k", "t"}
    schema = llm.generation_kwargs[0]["response_schema"]
    assert isinstance(schema, dict)
    assert schema["properties"]["tags"]["minItems"] == 3
    assert [
        branch["properties"]["tag_path"]["const"]
        for branch in schema["properties"]["tags"]["items"]["anyOf"]
    ] == request_payload["k"]
    assert llm.generation_kwargs[0]["max_tokens"] == 512


@pytest.mark.asyncio
async def test_candidate_prompt_content_is_part_of_the_actual_model_message() -> None:
    llm = _StructuredTagLLM(
        {
            "tags": [
                {"tag_path": "quality.greeting", "value": "pass", "confidence": 0.98},
            ]
        }
    )

    result = await LegacyTagBatcher(llm).classify(
        tenant_id="tenant-a",
        recording_id=42,
        transcript="坐席：您好。",
        tag_paths=("quality.greeting",),
        prompt_version="candidate-v2",
        prompt_content="候选规则：只有明确问候才判定通过。",
    )

    assert "候选规则：只有明确问候才判定通过。" in llm.calls[0][0]["content"]
    assert result.estimated_input_tokens > 0


@pytest.mark.asyncio
async def test_batch_rejects_missing_or_duplicate_tag_paths() -> None:
    missing = _StructuredTagLLM(
        {
            "tags": [
                {"tag_path": "quality.greeting", "value": "pass", "confidence": 0.9},
            ]
        }
    )
    batcher = LegacyTagBatcher(missing)

    with pytest.raises(LegacyTagBatchError, match="exactly one result"):
        await batcher.classify(
            tenant_id="tenant-a",
            recording_id=42,
            transcript="转写",
            tag_paths=("quality.greeting", "quality.closing"),
            prompt_version="v1",
        )

    duplicate = _StructuredTagLLM(
        {
            "tags": [
                {"tag_path": "quality.greeting", "value": "pass", "confidence": 0.9},
                {"tag_path": "quality.greeting", "value": "fail", "confidence": 0.8},
            ]
        }
    )
    with pytest.raises(LegacyTagBatchError, match="exactly one result"):
        await LegacyTagBatcher(duplicate).classify(
            tenant_id="tenant-a",
            recording_id=42,
            transcript="转写",
            tag_paths=("quality.greeting",),
            prompt_version="v1",
        )


@pytest.mark.asyncio
async def test_batch_rejects_invalid_value_and_confidence() -> None:
    invalid = _StructuredTagLLM(
        {
            "tags": [
                {
                    "tag_path": "quality.greeting",
                    "value": "maybe",
                    "confidence": 1.2,
                }
            ]
        }
    )

    with pytest.raises(LegacyTagBatchError):
        await LegacyTagBatcher(invalid).classify(
            tenant_id="tenant-a",
            recording_id=42,
            transcript="转写",
            tag_paths=("quality.greeting",),
            prompt_version="v1",
        )


@pytest.mark.asyncio
async def test_gateway_request_carries_the_strict_batch_response_validator() -> None:
    class _CapturingGateway:
        model = "gateway-weak"

        def __init__(self) -> None:
            self.request = None

        async def execute(self, request):
            self.request = request
            return LLMResponse(
                text=json.dumps(
                    {
                        "tags": [
                            {
                                "tag_path": "quality.greeting",
                                "value": "pass",
                                "confidence": 0.95,
                            }
                        ]
                    }
                ),
                model=self.model,
                prompt_hash="gateway-hash",
            )

    gateway = _CapturingGateway()
    await LegacyTagBatcher(gateway).classify(  # type: ignore[arg-type]
        tenant_id="tenant-a",
        recording_id=42,
        transcript="您好",
        tag_paths=("quality.greeting",),
        prompt_version="v1",
    )

    assert gateway.request is not None
    validator = gateway.request.response_validator
    assert validator is not None
    assert (
        validator(
            LLMResponse(
                text='{"tags":[{"tag_path":"quality.greeting","value":"pass","confidence":0.9}]}',
                model=gateway.model,
                prompt_hash="valid",
            )
        )
        is True
    )
    with pytest.raises(LegacyTagBatchError):
        validator(
            LLMResponse(
                text='{"tags":[]}',
                model=gateway.model,
                prompt_hash="invalid",
            )
        )
