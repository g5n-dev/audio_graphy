"""Extra coverage tests for ``audio_graphy.services.clap_service`` lifespan.

These install stubs for ``torch.cuda`` (returning True), ``laion_clap`` module,
and ``sys.exit`` so the lifespan code branches execute without GPU hardware
or model download.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest


def _install_torch_stub_with_cuda() -> Any:
    """Install torch stub where ``cuda.is_available()`` returns True."""
    torch_stub = types.ModuleType("torch")
    torch_stub._ag_stub = True  # type: ignore[attr-defined]

    class _CudaNS:
        @staticmethod
        def is_available() -> bool:
            return True

    torch_stub.cuda = _CudaNS()  # type: ignore[attr-defined]
    sys.modules["torch"] = torch_stub
    return torch_stub


def _install_laion_clap_stub(*, fail_load: bool = False) -> Any:
    """Install a fake ``laion_clap.CLAP_Module`` class."""
    mod = types.ModuleType("laion_clap")

    class _FakeClap:
        def __init__(self, *, enable_fusion: bool, amodel: str) -> None:
            self.enable_fusion = enable_fusion
            self.amodel = amodel

        def load_ckpt(self) -> None:
            if fail_load:
                raise RuntimeError("stub: model download failed")

    mod.CLAP_Module = _FakeClap  # type: ignore[attr-defined]
    sys.modules["laion_clap"] = mod
    return mod


@pytest.fixture
def clean_torch() -> Any:
    """Restore torch + laion_clap modules after each test."""
    saved_torch = sys.modules.get("torch")
    saved_laion = sys.modules.get("laion_clap")
    yield
    if saved_torch is None:
        sys.modules.pop("torch", None)
    else:
        sys.modules["torch"] = saved_torch
    if saved_laion is None:
        sys.modules.pop("laion_clap", None)
    else:
        sys.modules["laion_clap"] = saved_laion


# ============================================================
# lifespan: GPU available + happy path loads model
# ============================================================


def test_lifespan_happy_path(clean_torch, caplog) -> None:
    """When CUDA available + laion_clap loads, lifespan completes and yields."""
    # Re-import service so it picks up our torch stub via the lifespan import.
    _install_torch_stub_with_cuda()
    _install_laion_clap_stub()
    import audio_graphy.services.clap_service as svc

    svc._CLAP_MODEL = None  # reset

    async def _drive() -> None:
        async with svc.lifespan(svc.app):  # type: ignore[arg-type]
            # Inside lifespan: model should be loaded.
            assert svc._CLAP_MODEL is not None
            assert svc._CLAP_MODEL.amodel == "HTSAT-base"

    with caplog.at_level("INFO", logger="audio_graphy.services.clap_service"):
        asyncio.run(_drive())

    # After lifespan exit, model is released.
    assert svc._CLAP_MODEL is None
    assert any("CLAP model loaded" in r.message for r in caplog.records)
    assert any("CLAP model released" in r.message for r in caplog.records)


def test_lifespan_exits_when_no_cuda(clean_torch) -> None:
    """When ``torch.cuda.is_available()`` is False, lifespan calls sys.exit(1)."""

    # Install a torch stub returning False for cuda.
    torch_stub = types.ModuleType("torch")
    torch_stub._ag_stub = True  # type: ignore[attr-defined]

    class _CudaNS:
        @staticmethod
        def is_available() -> bool:
            return False

    torch_stub.cuda = _CudaNS()  # type: ignore[attr-defined]
    sys.modules["torch"] = torch_stub

    import audio_graphy.services.clap_service as svc

    # Stub sys.exit so it raises SystemExit instead of halting the process.
    real_exit = sys.exit

    exit_calls: list[int] = []

    def _fake_exit(code: int = 0) -> None:
        exit_calls.append(code)
        raise SystemExit(code)

    sys.exit = _fake_exit  # type: ignore[assignment]
    try:
        with pytest.raises(SystemExit):
            asyncio.run(svc.lifespan(svc.app).__aenter__())  # type: ignore[attr-defined]
        assert exit_calls == [1]
    finally:
        sys.exit = real_exit  # type: ignore[assignment]


def test_lifespan_exits_when_clap_load_fails(clean_torch) -> None:
    """When laion_clap model load raises, lifespan exits with code 1."""
    _install_torch_stub_with_cuda()
    _install_laion_clap_stub(fail_load=True)

    import audio_graphy.services.clap_service as svc

    real_exit = sys.exit
    exit_calls: list[int] = []

    def _fake_exit(code: int = 0) -> None:
        exit_calls.append(code)
        raise SystemExit(code)

    sys.exit = _fake_exit  # type: ignore[assignment]
    try:
        with pytest.raises(SystemExit):
            asyncio.run(svc.lifespan(svc.app).__aenter__())  # type: ignore[attr-defined]
        assert exit_calls == [1]
    finally:
        sys.exit = real_exit  # type: ignore[assignment]


# ============================================================
# /v1/audio/embed dim-mismatch path
# ============================================================
#
# Note: the dim-mismatch code path requires librosa to first decode the
# uploaded audio bytes (librosa.load inside the embed handler). Without
# librosa installed, the handler hits the librosa ImportError before the
# dim check; that path is therefore exercised by the existing
# ``test_clap_service.py`` happy-path test which is auto-skipped when
# librosa is missing. We don't duplicate it here.
