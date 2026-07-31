"""Bounded configuration and observation helpers for the semantic-tag Harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Literal


class HarnessSpecError(ValueError):
    """Raised when a Harness asks for an unregistered or unsafe action."""


_SECTIONS = frozenset({"context", "tools", "generation", "orchestration", "memory", "output"})
_SECTION_FIELDS: dict[str, frozenset[str]] = {
    "context": frozenset({"neighbor_units", "example_policy", "example_top_k"}),
    "tools": frozenset({"registered_tools", "primary_model", "critic_model"}),
    "generation": frozenset(
        {
            "temperature",
            "max_input_tokens",
            "max_tokens",
            "response_format",
            "prompt_template",
            "budget_policy",
        }
    ),
    "orchestration": frozenset(
        {
            "route",
            "fusion_policy",
            "critic_enabled",
            "rule_bundle",
            "rule_min_confidence",
            "critic_confidence_margin",
            "critic_max_noncritical_rate",
        }
    ),
    "memory": frozenset({"policy", "top_k"}),
    "output": frozenset(
        {
            "thresholds",
            "fallback",
            "schema_validation",
            "evidence_validation",
            "abstain_threshold",
            "review_threshold",
        }
    ),
}
_ROUTES = frozenset(
    {
        "rule_only",
        "weak_llm",
        "weak_then_strong_critic",
        "rule_llm_fusion",
    }
)
_FUSION_POLICIES = frozenset({"rule_priority", "score_priority", "conflict_to_review"})
_EXAMPLE_POLICIES = frozenset({"none", "similar", "hard_negative", "mixed"})
_MEMORY_POLICIES = frozenset({"none", "approved_cases"})
_REGISTERED_MODELS = frozenset({"weak", "strong"})
_REGISTERED_TOOLS = ("rule_engine", "weak_llm", "strong_llm")
_SOURCE_PRIORITY = {"rule": 0, "critic": 1, "weak": 2}
_OUTPUT_TOKEN_BUCKETS = (256, 512, 1024, 2048)
_PROMPT_DELTA_TOKEN_BUDGET = 512
_BUDGET_POLICY_FIELDS = frozenset(
    {
        "max_provider_tokens",
        "max_provider_calls",
        "max_cost_microunits",
        "max_wall_seconds",
    }
)


def _legacy_route(engine: object) -> str:
    return {
        "rule": "rule_only",
        "llm": "weak_llm",
        "hybrid": "rule_llm_fusion",
    }.get(str(engine), "rule_llm_fusion")


def _defaults(tagger: object) -> dict[str, Any]:
    return {
        "context": {
            "neighbor_units": 0,
            "example_policy": "none",
            "example_top_k": 0,
        },
        "tools": {
            "registered_tools": list(_REGISTERED_TOOLS),
            "primary_model": "weak",
            "critic_model": None,
        },
        "generation": {
            "temperature": 0,
            "max_input_tokens": 12_000,
            "max_tokens": 2048,
            "response_format": "strict_json",
            "prompt_template": str(getattr(tagger, "prompt_content", "") or ""),
            "budget_policy": {
                "max_provider_tokens": None,
                "max_provider_calls": None,
                "max_cost_microunits": None,
                "max_wall_seconds": None,
            },
        },
        "orchestration": {
            "route": _legacy_route(getattr(tagger, "engine", "hybrid")),
            "fusion_policy": "rule_priority",
            "critic_enabled": False,
            "rule_bundle": deepcopy(getattr(tagger, "rule_bundle", {}) or {}),
            "rule_min_confidence": 0.95,
            "critic_confidence_margin": 0.10,
            "critic_max_noncritical_rate": 0.20,
        },
        "memory": {
            "policy": "none",
            "top_k": 0,
        },
        "output": {
            "thresholds": deepcopy(getattr(tagger, "thresholds", {}) or {}),
            "fallback": "review",
            "schema_validation": True,
            "evidence_validation": True,
            "abstain_threshold": 0.0,
            "review_threshold": 0.7,
        },
    }


def _number_in_unit_interval(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= float(value) <= 1


def _section(spec: dict[str, Any], name: str) -> dict[str, Any]:
    value = spec.get(name)
    if value is None:
        value = {}
        spec[name] = value
    if not isinstance(value, Mapping):
        raise HarnessSpecError(f"harness section {name} must be an object")
    copied = deepcopy(dict(value))
    spec[name] = copied
    return copied


def _normalize_route(value: object) -> object:
    return {
        "rule": "rule_only",
        "llm": "weak_llm",
        "hybrid": "rule_llm_fusion",
        "weak_strong_critic": "weak_then_strong_critic",
    }.get(str(value), value)


def _normalize_compat_spec(
    raw_spec: Mapping[str, Any],
    *,
    tagger: object,
) -> dict[str, Any]:
    """Translate the two pre-canonical V1 draft shapes into the stable contract.

    These aliases are intentionally finite. Unknown keys still reach
    ``_validate_spec`` and are rejected, so compatibility cannot expand the
    runtime action space.
    """

    normalized = deepcopy(dict(raw_spec))
    context = _section(normalized, "context")
    tools = _section(normalized, "tools")
    generation = _section(normalized, "generation")
    orchestration = _section(normalized, "orchestration")
    memory = _section(normalized, "memory")
    output = _section(normalized, "output")

    if "neighbor_window" in context and "neighbor_units" not in context:
        context["neighbor_units"] = context["neighbor_window"]
    context.pop("neighbor_window", None)

    legacy_example_count = context.pop("example_count", None)
    legacy_example_strategy = context.pop("example_strategy", None)
    optimizer_example_count = memory.pop("example_count", None)
    optimizer_example_strategy = memory.pop("strategy", None)
    example_count = (
        legacy_example_count if legacy_example_count is not None else optimizer_example_count
    )
    example_strategy = (
        legacy_example_strategy
        if legacy_example_strategy is not None
        else optimizer_example_strategy
    )
    if example_count is not None:
        context.setdefault("example_top_k", example_count)
        context.setdefault(
            "example_policy",
            "none" if example_count == 0 else (example_strategy or "similar"),
        )

    if "max_output_tokens" in generation and "max_tokens" not in generation:
        generation["max_tokens"] = generation["max_output_tokens"]
    generation.pop("max_output_tokens", None)

    if "mode" in orchestration and "route" not in orchestration:
        orchestration["route"] = orchestration["mode"]
    orchestration.pop("mode", None)
    if "route" in orchestration:
        orchestration["route"] = _normalize_route(orchestration["route"])
    if "fusion" in orchestration and "fusion_policy" not in orchestration:
        orchestration["fusion_policy"] = orchestration["fusion"]
    orchestration.pop("fusion", None)

    legacy_critic_enabled = tools.pop("critic_enabled", None)
    tools.pop("rule_engine_enabled", None)
    tools.pop("weak_model", None)
    tools.pop("strong_model", None)
    if legacy_critic_enabled is not None:
        orchestration.setdefault("critic_enabled", legacy_critic_enabled)

    legacy_memory_enabled = memory.pop("enabled", None)
    legacy_retrieval_strategy = memory.pop("retrieval_strategy", None)
    if legacy_memory_enabled is not None:
        memory.setdefault(
            "policy",
            (
                str(legacy_retrieval_strategy or "approved_cases")
                if legacy_memory_enabled
                else "none"
            ),
        )
        if not legacy_memory_enabled:
            memory["top_k"] = 0

    output_aliases = {
        "validate_schema": "schema_validation",
        "evidence_required": "evidence_validation",
        "default_confidence_threshold": "review_threshold",
        "abstention_threshold": "abstain_threshold",
    }
    for alias, canonical in output_aliases.items():
        if alias in output and canonical not in output:
            output[canonical] = output[alias]
        output.pop(alias, None)

    threshold_offset = output.pop("threshold_offset", None)
    if threshold_offset is not None:
        if (
            isinstance(threshold_offset, bool)
            or not isinstance(threshold_offset, int | float)
            or not -1 <= float(threshold_offset) <= 1
        ):
            raise HarnessSpecError("output.threshold_offset must be between -1 and 1")
        base_thresholds = output.get("thresholds")
        if not isinstance(base_thresholds, Mapping):
            base_thresholds = getattr(tagger, "thresholds", {}) or {}
        output["thresholds"] = {
            str(key): min(1.0, max(0.0, float(value) + float(threshold_offset)))
            for key, value in base_thresholds.items()
        }

    route = orchestration.get("route")
    if route == "weak_then_strong_critic":
        orchestration.setdefault("critic_enabled", True)
        tools.setdefault("critic_model", "strong")
    return normalized


def _validate_spec(spec: Mapping[str, Any]) -> None:
    unknown_sections = set(spec) - _SECTIONS
    if unknown_sections:
        raise HarnessSpecError(f"unknown harness sections: {', '.join(sorted(unknown_sections))}")
    for section, value in spec.items():
        if not isinstance(value, Mapping):
            raise HarnessSpecError(f"harness section {section} must be an object")
        unknown_fields = set(value) - _SECTION_FIELDS[section]
        if unknown_fields:
            raise HarnessSpecError(f"unknown {section} fields: {', '.join(sorted(unknown_fields))}")

    context = spec.get("context", {})
    if "neighbor_units" in context and context["neighbor_units"] not in {0, 1, 2}:
        raise HarnessSpecError("context.neighbor_units must be 0, 1 or 2")
    if "example_policy" in context and context["example_policy"] not in _EXAMPLE_POLICIES:
        raise HarnessSpecError("context.example_policy is not registered")
    if "example_top_k" in context and context["example_top_k"] not in {0, 3, 6}:
        raise HarnessSpecError("context.example_top_k must be 0, 3 or 6")

    tools = spec.get("tools", {})
    if "primary_model" in tools and tools["primary_model"] not in _REGISTERED_MODELS:
        raise HarnessSpecError("tools.primary_model is not registered")
    critic_model = tools.get("critic_model")
    if critic_model is not None and critic_model not in _REGISTERED_MODELS:
        raise HarnessSpecError("tools.critic_model is not registered")
    if "registered_tools" in tools:
        registered_tools = tools["registered_tools"]
        if (
            not isinstance(registered_tools, Sequence)
            or isinstance(registered_tools, str | bytes)
            or tuple(registered_tools) != _REGISTERED_TOOLS
        ):
            raise HarnessSpecError("tools.registered_tools cannot be expanded by a version")

    generation = spec.get("generation", {})
    if "temperature" in generation and generation["temperature"] != 0:
        raise HarnessSpecError("generation.temperature is fixed to 0")
    if "max_input_tokens" in generation and generation["max_input_tokens"] != 12_000:
        raise HarnessSpecError("generation.max_input_tokens must be 12000")
    if "max_tokens" in generation and generation["max_tokens"] not in _OUTPUT_TOKEN_BUCKETS:
        raise HarnessSpecError("generation.max_tokens must be 256, 512, 1024 or 2048")
    if "response_format" in generation and generation["response_format"] != "strict_json":
        raise HarnessSpecError("generation.response_format must be strict_json")
    if "prompt_template" in generation and not isinstance(
        generation["prompt_template"],
        str,
    ):
        raise HarnessSpecError("generation.prompt_template must be a string")
    if "budget_policy" in generation:
        budget_policy = generation["budget_policy"]
        if not isinstance(budget_policy, Mapping):
            raise HarnessSpecError("generation.budget_policy must be an object")
        unknown_budget_fields = set(budget_policy) - _BUDGET_POLICY_FIELDS
        if unknown_budget_fields:
            raise HarnessSpecError(
                "unknown generation.budget_policy fields: "
                + ", ".join(sorted(unknown_budget_fields))
            )
        for field, value in budget_policy.items():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise HarnessSpecError(
                    f"generation.budget_policy.{field} must be a positive integer or null"
                )

    orchestration = spec.get("orchestration", {})
    if "route" in orchestration and orchestration["route"] not in _ROUTES:
        raise HarnessSpecError("orchestration.route is not registered")
    if "fusion_policy" in orchestration and orchestration["fusion_policy"] not in _FUSION_POLICIES:
        raise HarnessSpecError("orchestration.fusion_policy is not registered")
    for field, required in (
        ("rule_min_confidence", 0.95),
        ("critic_confidence_margin", 0.10),
        ("critic_max_noncritical_rate", 0.20),
    ):
        if field in orchestration and orchestration[field] != required:
            raise HarnessSpecError(f"orchestration.{field} is fixed to {required:g} in Harness v2")

    memory = spec.get("memory", {})
    if "policy" in memory and memory["policy"] not in _MEMORY_POLICIES:
        raise HarnessSpecError("memory.policy is not registered")
    if "top_k" in memory and memory["top_k"] not in {0, 3, 6}:
        raise HarnessSpecError("memory.top_k must be 0, 3 or 6")

    output = spec.get("output", {})
    if "fallback" in output and output["fallback"] not in {"review", "abstain", "rule"}:
        raise HarnessSpecError("output.fallback is not registered")
    for field in ("abstain_threshold", "review_threshold"):
        if field in output and not _number_in_unit_interval(output[field]):
            raise HarnessSpecError(f"output.{field} must be between 0 and 1")
    if "thresholds" in output:
        thresholds = output["thresholds"]
        if not isinstance(thresholds, Mapping) or any(
            not _number_in_unit_interval(value) for value in thresholds.values()
        ):
            raise HarnessSpecError("output.thresholds must contain probabilities")


def resolve_harness_spec(tagger: object) -> dict[str, Any]:
    """Normalize V1/V2 Tagger fields into the bounded V2 runtime contract."""

    raw = getattr(tagger, "harness_spec", {}) or {}
    if not isinstance(raw, Mapping):
        raise HarnessSpecError("harness_spec must be an object")
    raw_spec = deepcopy(dict(raw))
    spec_version = raw_spec.pop(
        "spec_version",
        getattr(tagger, "harness_spec_version", "1.0") or "1.0",
    )
    if spec_version not in {"1.0", "2.0"}:
        raise HarnessSpecError("only Harness spec_version 1.0 and 2.0 are supported")
    raw_spec = _normalize_compat_spec(raw_spec, tagger=tagger)
    _validate_spec(raw_spec)
    resolved = _defaults(tagger)
    for section, values in raw_spec.items():
        resolved[section].update(deepcopy(dict(values)))
    _validate_spec(resolved)

    route = resolved["orchestration"]["route"]
    critic_enabled = bool(resolved["orchestration"]["critic_enabled"])
    critic_model = resolved["tools"]["critic_model"]
    if route == "weak_then_strong_critic" and (not critic_enabled or critic_model != "strong"):
        raise HarnessSpecError(
            "weak_then_strong_critic requires critic_enabled and the strong critic"
        )
    if resolved["context"]["example_policy"] == "none":
        resolved["context"]["example_top_k"] = 0
    return resolved


def output_token_budget(
    tag_count: int,
    *,
    configured_cap: int = 2048,
) -> int:
    """Return ``ceil256(128 + 96 * tags)`` in the registered output buckets."""

    if isinstance(tag_count, bool) or not isinstance(tag_count, int) or tag_count < 0:
        raise HarnessSpecError("tag_count must be a non-negative integer")
    if configured_cap not in _OUTPUT_TOKEN_BUCKETS:
        raise HarnessSpecError("configured output cap must be 256, 512, 1024 or 2048")
    requested = 128 + (96 * tag_count)
    bucket = next(
        (value for value in _OUTPUT_TOKEN_BUCKETS if value >= requested),
        _OUTPUT_TOKEN_BUCKETS[-1],
    )
    return min(bucket, configured_cap)


def estimate_prompt_tokens(value: str) -> int:
    """Conservative, tokenizer-independent proxy suitable for a hard preflight."""

    if not isinstance(value, str):
        raise HarnessSpecError("prompt content must be a string")
    if not value:
        return 0
    ascii_count = sum(character.isascii() for character in value)
    non_ascii_count = len(value) - ascii_count
    character_proxy = non_ascii_count + ((ascii_count + 3) // 4)
    byte_proxy = (len(value.encode("utf-8")) + 3) // 4
    return max(character_proxy, byte_proxy)


def materialize_trial_candidate(
    baseline_config: Mapping[str, Any],
    *,
    prompt_delta: str = "",
    rule_bundle: Mapping[str, Any] | None = None,
    max_prompt_delta_tokens: int = _PROMPT_DELTA_TOKEN_BUDGET,
) -> dict[str, Any]:
    """Freeze prompt/rule changes into the exact config evaluated by a trial."""

    if (
        isinstance(max_prompt_delta_tokens, bool)
        or not isinstance(max_prompt_delta_tokens, int)
        or not 1 <= max_prompt_delta_tokens <= _PROMPT_DELTA_TOKEN_BUDGET
    ):
        raise HarnessSpecError("prompt delta budget must be between 1 and 512 tokens")
    if not isinstance(prompt_delta, str):
        raise HarnessSpecError("prompt delta must be a string")
    normalized_delta = prompt_delta.strip()
    token_estimate = estimate_prompt_tokens(normalized_delta)
    if token_estimate > max_prompt_delta_tokens:
        raise HarnessSpecError(
            f"prompt delta exceeds the {max_prompt_delta_tokens}-token proxy budget"
        )

    candidate = deepcopy(dict(baseline_config))
    generation = _section(candidate, "generation")
    orchestration = _section(candidate, "orchestration")
    current_prompt = generation.get("prompt_template", "")
    if not isinstance(current_prompt, str):
        raise HarnessSpecError("generation.prompt_template must be a string")
    if normalized_delta:
        generation["prompt_template"] = (
            f"{current_prompt.rstrip()}\n\n{normalized_delta}"
            if current_prompt.strip()
            else normalized_delta
        )
    if rule_bundle is not None:
        if not isinstance(rule_bundle, Mapping):
            raise HarnessSpecError("rule bundle must be an object")
        orchestration["rule_bundle"] = deepcopy(dict(rule_bundle))
    _validate_spec(candidate)
    return candidate


def build_scene_profile(
    *,
    scenario: str,
    subject_type: str,
    store_id: str | None = None,
    transcript: str,
    segments: Sequence[object],
) -> dict[str, Any]:
    """Build a stable V1 profile without pretending unavailable audio signals exist."""

    starts = [
        float(value)
        for segment in segments
        if (value := getattr(segment, "start_sec", None)) is not None
    ]
    ends = [
        float(value)
        for segment in segments
        if (value := getattr(segment, "end_sec", None)) is not None
    ]
    duration = max(ends) - min(starts) if starts and ends else 0.0
    speakers = {
        str(speaker) for segment in segments if (speaker := getattr(segment, "speaker", None))
    }
    vad_values = [
        float(value)
        for segment in segments
        if (value := getattr(segment, "vad_conf", None)) is not None
    ]
    average_vad = round(sum(vad_values) / len(vad_values), 6) if vad_values else None
    return {
        "scenario": scenario,
        "store_id": store_id,
        "subject_type": subject_type,
        "duration_sec": round(max(0.0, duration), 3),
        "segment_count": len(segments),
        "speaker_count": len(speakers),
        "average_vad_confidence": average_vad,
        "transcript_char_count": len("".join(transcript.split())),
        "snr": None,
        "overlap_ratio": None,
        "asr_confidence": None,
        "diarization_confidence": None,
    }


def build_stage_observation(
    *,
    status: Literal["success", "warning", "error"],
    summary: str,
    next_actions: Sequence[str] = (),
    artifacts: Sequence[str] = (),
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the stable observation/recovery envelope used by every stage."""

    return {
        "status": status,
        "summary": summary,
        "next_actions": list(next_actions),
        "artifacts": list(artifacts),
        "details": dict(details or {}),
    }


def fuse_assignments(
    candidates_by_source: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    policy: str,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """Fuse registered source outputs without hiding model/rule disagreements."""

    if policy not in _FUSION_POLICIES:
        raise HarnessSpecError("fusion policy is not registered")
    selected: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    tag_keys = sorted(
        {tag_key for candidates in candidates_by_source.values() for tag_key in candidates}
    )
    for tag_key in tag_keys:
        options = [
            (source, candidates[tag_key])
            for source, candidates in candidates_by_source.items()
            if tag_key in candidates
        ]
        distinct_values = {repr(option.get("tag_value")) for _source, option in options}
        if len(distinct_values) > 1:
            conflicts.append(tag_key)
            if policy == "conflict_to_review":
                continue

        if policy == "rule_priority":
            source, winner = min(
                options,
                key=lambda item: (
                    _SOURCE_PRIORITY.get(item[0], 99),
                    -float(item[1].get("confidence", 0)),
                    item[0],
                ),
            )
            del source
        else:
            source, winner = max(
                options,
                key=lambda item: (
                    float(item[1].get("confidence", 0)),
                    -_SOURCE_PRIORITY.get(item[0], 99),
                    item[0],
                ),
            )
            del source
        selected[tag_key] = deepcopy(dict(winner))
    return selected, tuple(conflicts)


__all__ = [
    "HarnessSpecError",
    "build_scene_profile",
    "build_stage_observation",
    "estimate_prompt_tokens",
    "fuse_assignments",
    "materialize_trial_candidate",
    "output_token_budget",
    "resolve_harness_spec",
]
