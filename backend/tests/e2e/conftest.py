"""Re-export the API conftest fixtures for E2E tests.

The E2E directory sits next to ``tests/api/`` but does not have its
own ``test_client``/``auth_headers`` fixtures. We re-export them here
so E2E tests get the same setup.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``tests.api`` importable.
_TESTS_ROOT = Path(__file__).resolve().parent.parent
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

# Re-export the heavy fixtures so E2E tests get the same DB seeding.
from tests.api.conftest import (  # noqa: E402,F401
    api_settings,
    auth_headers,
    db_session_factory,
    jwt_manager,
    seed_recording,
    seed_segment,
    seed_tag,
    test_client,
)
