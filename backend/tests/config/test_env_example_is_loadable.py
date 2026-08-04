"""``.env.example`` must actually build a Settings object.

The documented first step is ``cp .env.example .env && docker compose up``, and
nothing verified that the result parses. It did not: ``RERANK_CHANNEL_WEIGHTS``
was written comma-separated, pydantic-settings parses every complex-typed field
as JSON, and the backend died inside ``alembic upgrade head`` with
``SettingsError`` before the container ever became healthy.

Nothing existing could catch that. The unit tests construct ``Settings`` with
keyword arguments, so they never read this file; CI's compose job runs
``docker compose config``, which renders YAML without starting a process; and a
developer whose ``.env`` predates the line never sees it. The only way to find it
was to start the stack from a clean clone -- which is what this test replaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values

from audio_graphy.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: Values compose interpolates into the YAML rather than handing to the app.
#: They are not Settings fields, so they are not this test's concern.
_COMPOSE_ONLY_PREFIXES = ("COMPOSE_", "SILERO_VAD_MODEL_FILE")


def _parse_env_example() -> dict[str, str]:
    """Parse it the way Settings does.

    ``dotenv_values`` is what pydantic-settings uses for ``env_file=".env"``, so
    it is also what decides whether a line here is valid — including trailing
    ``# comment`` handling, which a hand-rolled regex gets wrong in the strict
    direction and would fail this test over a file that loads fine.
    """

    return {key: value for key, value in dotenv_values(ENV_EXAMPLE).items() if value is not None}


def test_env_example_exists_and_is_not_empty() -> None:
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} is missing"
    assert _parse_env_example(), "no KEY=VALUE lines parsed"


def test_every_value_in_env_example_parses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Load the file the way pydantic-settings would and build Settings.

    Set as real environment variables rather than passed as kwargs: kwargs skip
    ``EnvSettingsSource`` entirely, which is the layer that does the JSON parsing
    and the layer that raised.
    """

    for key, raw in _parse_env_example().items():
        if key.startswith(_COMPOSE_ONLY_PREFIXES):
            continue
        monkeypatch.setenv(key, raw)

    # Paths only: the example points at container paths that do not exist here,
    # and this test is about parsing, not about the filesystem.
    monkeypatch.setenv("WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("MASTER_KEY_PATH", str(tmp_path / "master.key"))

    settings = Settings()

    # Spot-check the field that failed, so a regression names itself.
    assert settings.rerank_channel_weights == (0.5, 0.3, 0.2)


def test_complex_typed_fields_are_written_as_json() -> None:
    """Catch the next one before it reaches a container.

    A tuple/list/dict field written comma-separated raises ``SettingsError``, not
    a validation error, so the message does not say what is wrong with the value
    — only which field it came from. Checking the shape here says it plainly.
    """

    complex_keys = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if any(
            token in str(field.annotation)
            for token in ("tuple", "list", "dict", "set", "Tuple", "List", "Dict")
        )
    }
    offenders = [
        f"{key}={raw}"
        for key, raw in _parse_env_example().items()
        if key in complex_keys and not raw.strip().startswith(("[", "{", '"'))
    ]
    assert not offenders, (
        "complex-typed settings must be JSON in .env.example, not comma-separated: "
        + ", ".join(offenders)
    )
