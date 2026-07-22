"""Re-export API conftest fixtures for the M1-M8 regression suite."""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent.parent
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from tests.api.conftest import (  # noqa: E402,F401
    api_settings,
    auth_headers,
    db_session_factory,
    jwt_manager,
    seed_recording,
    test_client,
)
