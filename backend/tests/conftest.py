"""Pytest configuration and shared fixtures for AudioGraphy backend tests.

Design principles (per docs/DESIGN.md §7):
- Tests live alongside modules (audio_graphy/core/X.py ↔ tests/core/test_X.py)
- TDD: write failing test first, then implement.
- Five layers: unit / integration / contract / e2e / perf (see markers in pyproject.toml)
- Coverage gate: 85% line+branch enforced via --cov-fail-under.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Ensure backend/ is importable when tests run from project root.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Force a test-friendly default environment BEFORE audio_graphy.config is imported.
# Individual tests can override via monkeypatch or fixture.
os.environ.setdefault("ADAPTER_MODE", "mock")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3306")
os.environ.setdefault("MYSQL_USER", "audiography")
os.environ.setdefault("MYSQL_PASSWORD", "change-me")
os.environ.setdefault("MYSQL_DB", "audiography_test")
os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-minimum-length!!")
os.environ.setdefault("WORKING_DIR", "/tmp/audiography_test_working_dir")
# The suite builds apps whose DB is a stub or absent; production refuses to
# start in that state, so opt the tests out of the strict check explicitly.
os.environ.setdefault("ALLOW_DEGRADED_STARTUP", "true")
os.environ.setdefault("DEFAULT_TENANT_ID", "default")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")


# ============================================================
# Event loop — function-scoped by default for asyncio tests
# ============================================================
@pytest.fixture
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Function-scoped event loop per pytest-asyncio best practice."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ============================================================
# Working directory — temp dir per test session
# ============================================================
@pytest.fixture
def tmp_working_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Per-session temp directory standing in for WORKING_DIR."""
    return tmp_path_factory.mktemp("working_dir")


# ============================================================
# Settings — fresh instance per test (clears lru_cache)
# ============================================================
@pytest.fixture
def fresh_settings(monkeypatch: pytest.MonkeyPatch, tmp_working_dir: Path):
    """Reset get_settings cache and point WORKING_DIR at a temp dir."""
    from audio_graphy.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("WORKING_DIR", str(tmp_working_dir))
    monkeypatch.setenv("ADAPTER_MODE", "mock")
    return get_settings()


# ============================================================
# Mock adapter bundle — pre-wired mock adapters, no real services
# ============================================================
@pytest.fixture
def mock_adapters(fresh_settings):
    """Return an AdapterBundle with all four adapters as Mock implementations."""
    from audio_graphy.config import build_adapters

    return build_adapters(fresh_settings)


# ============================================================
# Common test data — Chinese store conversation fixtures
# ============================================================
STORE_SCRIPT_FIXTURE = """\
[00:00.000 --> 00:08.420] 坐席: 您好，欢迎光临，我是销售顾问张敏，请问您今天看什么车型？
[00:08.420 --> 00:14.800] 客户: 我想了解一下 CS75 Plus，听说最近有优惠。
[00:14.800 --> 00:25.100] 坐席: 是的，CS75 Plus 现在全款优惠 5 万元，还可以选 36 期分期，
方案搭配 2 年免息，另外赠送 3 次保养。
[00:25.100 --> 00:33.500] 客户: 那 UNI-V 呢？我朋友开的是 UNI-V，感觉也不错。
[00:33.500 --> 00:45.200] 坐席: UNI-V 也有 36 期分期方案，但金融政策略有不同。我们对比
一下，看哪款更适合您。
"""


@pytest.fixture
def store_script_text() -> str:
    """A short scripted Chinese car-sale conversation for fixture-based tests."""
    return STORE_SCRIPT_FIXTURE
