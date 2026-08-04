"""The probe must answer without importing what it is probing.

That constraint is the entire reason this module exists. ``import dspy`` drags in
litellm, which warns at import time, and the suite runs under
``filterwarnings = ["error"]`` -- so an import-based probe would make "DSPy is
installed" indistinguishable from "the test suite is broken".
"""

from __future__ import annotations

import sys

import pytest

from audio_graphy.optimizers.availability import (
    DSPY_DISTRIBUTION,
    TEXTGRAD_DISTRIBUTION,
    MissingExtraError,
    dspy_status,
    probe,
    textgrad_status,
)


def test_a_distribution_that_is_not_installed_is_reported_as_absent() -> None:
    status = probe("audio-graphy-no-such-distribution")

    assert status.installed is False
    assert status.version is None


def test_an_installed_distribution_reports_its_version() -> None:
    status = probe("pytest")

    assert status.installed is True
    assert status.version == pytest.__version__


def test_probing_never_imports_the_distribution_it_asks_about() -> None:
    """A probe with import side effects would defeat its own purpose."""

    before = set(sys.modules)

    probe(DSPY_DISTRIBUTION)
    probe(TEXTGRAD_DISTRIBUTION)

    newly_imported = {
        name
        for name in set(sys.modules) - before
        if name.split(".")[0] in {"dspy", "textgrad", "litellm"}
    }
    assert newly_imported == set()


def test_requiring_a_missing_extra_says_how_to_install_it() -> None:
    status = probe("audio-graphy-no-such-distribution")

    with pytest.raises(MissingExtraError, match=r"\[optimizer\]"):
        status.require()


def test_requiring_an_installed_extra_returns_the_version() -> None:
    assert probe("pytest").require() == pytest.__version__


def test_the_named_helpers_probe_the_distributions_the_extras_declare() -> None:
    # A typo here would report the optional extra as permanently missing, and the
    # worker would silently keep falling back to the builtin proposer.
    assert dspy_status().distribution == DSPY_DISTRIBUTION
    assert textgrad_status().distribution == TEXTGRAD_DISTRIBUTION
