"""Graph explorer response-budget configuration tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from audio_graphy.config import Settings


@pytest.mark.unit
def test_graph_edge_render_budget_defaults_to_absolute_cap(tmp_path: Path) -> None:
    settings = Settings(working_dir=tmp_path)

    assert settings.graph_edge_render_budget == 5_000


@pytest.mark.unit
@pytest.mark.parametrize("budget", [0, 5_001])
def test_graph_edge_render_budget_rejects_unsafe_values(
    tmp_path: Path,
    budget: int,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            working_dir=tmp_path,
            graph_edge_render_budget=budget,
        )
