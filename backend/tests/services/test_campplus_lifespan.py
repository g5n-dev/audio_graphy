"""Coverage tests for ``audio_graphy.services.campplus_service`` lifespan.

Stubs out ``funasr.AutoModel`` so the lifespan branches run without needing the
real funasr package or model download. The property under test is the split
between the two model stacks: the SV model is fatal on failure, the ASR
pipeline is preloaded in the background and never fatal.
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


def _install_funasr_stub(*, sv_fail: bool = False, asr_fail: bool = False) -> Any:
    """Install a fake ``funasr`` module with a configurable ``AutoModel``.

    The two stacks are told apart by their model id, the same way the service
    builds them: the bare SV model versus the ASR pipeline that carries a
    ``spk_model`` kwarg.
    """
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
            self.spk_model = object() if kwargs.get("spk_model") else None
            is_asr = "paraformer" in model
            if sv_fail and not is_asr:
                raise RuntimeError("stub: SV load failed")
            if asr_fail and is_asr:
                raise RuntimeError("stub: ASR pipeline load failed")

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


async def _settle(svc: Any) -> None:
    """Wait for the background ASR preload to finish."""
    for _ in range(200):
        if svc._ASR_MODEL is not None or svc._ASR_LOAD_ERROR is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("ASR preload never settled")


def test_lifespan_happy_path_both_models(clean_funasr, monkeypatch) -> None:
    """SV loads inline; the ASR pipeline arrives via the background preload."""
    _install_funasr_stub()
    import audio_graphy.services.campplus_service as svc

    svc._SV_MODEL = None
    svc._ASR_MODEL = None
    svc._ASR_LOAD_ERROR = None
    monkeypatch.setenv("CAMPPLUS_DEVICE", "cpu")
    monkeypatch.setenv("CAMPPLUS_ASR_PRELOAD", "1")

    async def _drive() -> None:
        async with svc.lifespan(svc.app):  # type: ignore[arg-type]
            # SV is up the moment the app starts serving — it does not wait
            # on the ~2-minute pipeline load.
            assert svc._SV_MODEL is not None
            await _settle(svc)
            assert svc._ASR_MODEL is not None
            assert svc._ASR_LOAD_ERROR is None
            assert svc._spk_attached(svc._ASR_MODEL) is True

    asyncio.run(_drive())
    # After exit, both models released.
    assert svc._SV_MODEL is None
    assert svc._ASR_MODEL is None


def test_lifespan_skips_preload_when_disabled(clean_funasr, monkeypatch) -> None:
    """CAMPPLUS_ASR_PRELOAD=0 leaves the pipeline for the first request."""
    _install_funasr_stub()
    import audio_graphy.services.campplus_service as svc

    svc._SV_MODEL = None
    svc._ASR_MODEL = None
    svc._ASR_LOAD_ERROR = None
    monkeypatch.setenv("CAMPPLUS_ASR_PRELOAD", "0")

    async def _drive() -> None:
        async with svc.lifespan(svc.app):  # type: ignore[arg-type]
            await asyncio.sleep(0.05)
            assert svc._SV_MODEL is not None
            assert svc._ASR_MODEL is None
            assert svc._ASR_LOAD_ERROR is None

    asyncio.run(_drive())


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


def test_lifespan_survives_asr_pipeline_failure(clean_funasr, monkeypatch) -> None:
    """A failed ASR preload is recorded, not fatal, and never takes SV down."""
    _install_funasr_stub(asr_fail=True)
    import audio_graphy.services.campplus_service as svc

    svc._SV_MODEL = None
    svc._ASR_MODEL = None
    svc._ASR_LOAD_ERROR = None
    monkeypatch.setenv("CAMPPLUS_ASR_PRELOAD", "1")

    async def _drive() -> None:
        async with svc.lifespan(svc.app):  # type: ignore[arg-type]
            await _settle(svc)
            assert svc._SV_MODEL is not None  # voiceprint still serves
            assert svc._ASR_MODEL is None
            assert "ASR pipeline load failed" in (svc._ASR_LOAD_ERROR or "")

    asyncio.run(_drive())


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
