"""Smoke test — confirms pytest can collect and run a trivial test.

This is a placeholder until M1.4 (models) lands real tests.
"""

from __future__ import annotations


def test_smoke_pytest_runs() -> None:
    """pytest must be able to discover and run this test."""
    assert 1 + 1 == 2


def test_python_version() -> None:
    """Target runtime is Python 3.13+."""
    import sys

    assert sys.version_info >= (3, 13), f"Expected Python >=3.13, got {sys.version_info}"
