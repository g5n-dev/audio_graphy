"""Coverage tests for ``audio_graphy.services.campplus_service`` lifespan.

Stubs out ``funasr.AutoModel`` so the lifespan branches run without needing
the real funasr package or model download. Targets lines 55-88 of
``campplus_service.py`` (lifespan function body).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest


def _install_torch_stub() -> None:
    if "torch" in sys.modules and getattr(sys.modules["torch"], "_ag_stub", False):
        return
    torch_stub = types.ModuleType("torch")
    torch_stub._ag_stub = True  # type: ignore[attr-defined]

    class _CudaNS:
        @staticmethod
        def is_available() -> bool:
            return False

    torch_stub.cuda = _CudaNS()  # type: ignore[attr-defined]
    sys.modules["torch"] = torch_stub


_install_torch_stub()


def _install_funasr_stub(*, sv_fail: bool = False, diarize_fail: bool = False) -> Any:
    """Install a fake ``funasr`` module with a configurable ``AutoModel``."""
    mod = types.ModuleType("funasr")
    mod._ag_stub = True  # type: ignore[attr-defined]

    class _FakeAutoModel:
        def __init__(
            self,
            *,
            model: str,
            device: str = "cpu",
            disable_update: bool = False,
            **kwargs: Any,
        ) -> None:
            self.model_name = model
            self.device = device
            # SV model id triggers failure when configured.
            if sv_fail and "campplus_sv" in model:
                raise RuntimeError("stub: SV load failed")
            if diarize_fail and "eres2sv" in model:
                raise RuntimeError("stub: diarize load failed")

    mod.AutoModel = _FakeAutoModel  # type: ignore[attr-defined]
    sys.modules["funasr"] = mod
    return mod


@pytest.fixture
def clean_funasr() -> Any:
    """Restore funasr module + service state after each test."""
    saved_funasr = sys.modules.get("funasr")
    yield
    if saved_funasr is None:
        sys.modules.pop("funasr", None)
    else:
        sys.modules["funasr"] = saved_funasr


def test_lifespan_happy_path_both_models(clean_funasr, monkeypatch) -> None:
    """When funasr is installed + both models load, lifespan completes."""
    _install_funasr_stub()
    import audio_graphy.services.campplus_service as svc

    svc._SV_MODEL = None
    svc._DIARIZE_MODEL = None
    monkeypatch.setenv("CAMPPLUS_DEVICE", "cpu")

    async def _drive() -> None:
        async with svc.lifespan(svc.app):  # type: ignore[arg-type]
            assert svc._SV_MODEL is not None
            assert svc._DIARIZE_MODEL is not None

    asyncio.run(_drive())
    # After exit, both models released.
    assert svc._SV_MODEL is None
    assert svc._DIARIZE_MODEL is None


def test_lifespan_exits_when_sv_load_fails(clean_funasr) -> None:
    """When SV model load raises, lifespan calls sys.exit(1)."""
    _install_funasr_stub(sv_fail=True)
    import audio_graphy.services.campplus_service as svc

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


def test_lifespan_continues_when_diarize_fails(clean_funasr, caplog) -> None:
    """When diarize model load fails, lifespan continues with warning."""
    _install_funasr_stub(diarize_fail=True)
    import audio_graphy.services.campplus_service as svc

    svc._SV_MODEL = None
    svc._DIARIZE_MODEL = None

    async def _drive() -> None:
        async with svc.lifespan(svc.app):  # type: ignore[arg-type]
            # SV loaded; diarize left as None.
            assert svc._SV_MODEL is not None
            assert svc._DIARIZE_MODEL is None

    with caplog.at_level(
        "WARNING", logger="audio_graphy.services.campplus_service"
    ):
        asyncio.run(_drive())

    assert any(
        "Diarization model" in r.message and "unavailable" in r.message
        for r in caplog.records
    )


def test_lifespan_exits_when_funasr_not_installed(clean_funasr) -> None:
    """When funasr is not installed, lifespan exits with code 1."""
    sys.modules.pop("funasr", None)

    # Block the import path: insert a finder that raises ImportError for funasr.
    class _BlockFunasr:
        def find_spec(self, name: str, path: Any = None) -> Any:
            if name == "funasr":
                raise ImportError("blocked by test")
            return None

    blocker = _BlockFunasr()
    sys.meta_path.insert(0, blocker)
    try:
        import audio_graphy.services.campplus_service as svc

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
    finally:
        sys.meta_path.remove(blocker)
