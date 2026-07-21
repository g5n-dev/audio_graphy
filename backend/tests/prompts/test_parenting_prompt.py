"""Tests for entity_zh_parenting prompt (v1.1) — 3 cases (M6 WS-3).

Verifies:
    1. The v1.1 entry is registered in ``prompts/versions.yaml`` with the
       correct ``scenario: parenting_consulting``.
    2. The prompt file loads and contains the GraphRAG delimiter protocol
       placeholders (``{tuple_delimiter}`` / ``{record_delimiter}`` /
       ``{completion_delimiter}`` / ``{entity_types}`` / ``{input_text}``).
    3. The scenario field is set correctly so the prompt loader can pick
       the right template by scenario.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "audio_graphy"
    / "prompts"
)


def _load_versions() -> dict:
    with (_PROMPTS_DIR / "versions.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------
# Case 1 — v1.1 registered in versions.yaml
# --------------------------------------------------------------------

def test_v1_1_registered_in_versions_yaml() -> None:
    """versions.yaml must contain entity_zh_parenting v1.1."""
    versions = _load_versions()
    assert "prompts" in versions
    parenting = versions["prompts"].get("entity_zh_parenting")
    assert parenting is not None, "entity_zh_parenting entry missing"
    assert "v1.1" in parenting
    v11 = parenting["v1.1"]
    assert v11["file"] == "entity_zh_parenting.md"
    assert v11["active"] is False  # v1.0 stays default
    assert "changelog" in v11 and v11["changelog"]


# --------------------------------------------------------------------
# Case 2 — prompt file loads and parses delimiter format
# --------------------------------------------------------------------

def test_prompt_file_loads_and_has_delimiters() -> None:
    """The v1.1 prompt file exists and contains all required placeholders."""
    prompt_path = _PROMPTS_DIR / "entity_zh_parenting.md"
    assert prompt_path.is_file(), f"prompt file missing: {prompt_path}"
    text = prompt_path.read_text(encoding="utf-8")

    # Required GraphRAG delimiter placeholders (matches entity_zh.md v1.0).
    required_placeholders = (
        "{tuple_delimiter}",
        "{record_delimiter}",
        "{completion_delimiter}",
        "{entity_types}",
        "{input_text}",
    )
    for ph in required_placeholders:
        assert ph in text, f"placeholder {ph!r} missing in parenting prompt"

    # Few-shot entity format: ("实体"{tuple_delimiter}名称{tuple_delimiter}类型{tuple_delimiter}描述)
    assert '("实体"{tuple_delimiter}' in text
    # Few-shot relation format: ("关系"{tuple_delimiter}源{tuple_delimiter}关系{tuple_delimiter}目标{tuple_delimiter}描述)
    assert '("关系"{tuple_delimiter}' in text


# --------------------------------------------------------------------
# Case 3 — scenario field set correctly
# --------------------------------------------------------------------

def test_scenario_field_parenting_consulting() -> None:
    """scenario field on v1.1 entry must be 'parenting_consulting'."""
    versions = _load_versions()
    v11 = versions["prompts"]["entity_zh_parenting"]["v1.1"]
    assert v11.get("scenario") == "parenting_consulting"


# --------------------------------------------------------------------
# Bonus — v1.0 entity_zh unchanged (regression check)
# --------------------------------------------------------------------

def test_v1_0_entity_zh_unchanged() -> None:
    """v1.0 entity_zh entry must remain active=true (default prompt)."""
    versions = _load_versions()
    v10 = versions["prompts"]["entity_zh"]["v1.0"]
    assert v10["file"] == "entity_zh.md"
    assert v10["active"] is True
    assert v10.get("scenario") == "automotive_sales"


@pytest.mark.parametrize(
    "scenario_keyword",
    ["家长", "顾问", "月龄", "育儿方法"],
)
def test_prompt_contains_parenting_entities(scenario_keyword: str) -> None:
    """Prompt body covers core parenting entity types."""
    text = (_PROMPTS_DIR / "entity_zh_parenting.md").read_text(encoding="utf-8")
    assert scenario_keyword in text
