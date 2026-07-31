"""Production eval entry points must not bypass the centralized LLM runtime."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


class _FakeEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


class _FakeRuntime:
    def __init__(self, bundle: object) -> None:
        self.bundle = bundle
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


def _patch_eval_components(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tmp_path: Path,
    observed: dict[str, Any],
    runner_error: Exception | None,
) -> None:
    import audio_graphy.eval.judge as judge_mod
    import audio_graphy.eval.reporter as reporter_mod
    import audio_graphy.eval.runner as runner_mod
    import audio_graphy.eval.state as state_mod

    run_row = SimpleNamespace(
        status="pending",
        gold_set_path=str(tmp_path / "gold.yaml"),
        pipeline="mock",
        config={},
        judge_enabled=True,
        k_value=5,
    )

    class FakeState:
        def __init__(self, session_factory: object) -> None:
            observed["state_factory"] = session_factory

        async def get(self, run_id: str, tenant_id: str) -> object:
            observed["state_get"] = (run_id, tenant_id)
            return run_row

        async def transition_to(self, run_id: str, status: str, **kwargs: object) -> None:
            observed.setdefault("transitions", []).append((run_id, status, kwargs))

    class FakeJudge:
        def __init__(self, llm: object) -> None:
            observed["judge_llm"] = llm

    class FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            observed["runner_kwargs"] = kwargs

        async def run(self) -> object:
            if runner_error is not None:
                raise runner_error
            return SimpleNamespace(run_id="eval-1", per_example=(), aggregate_metrics={})

    monkeypatch.setattr(state_mod, "EvalRunState", FakeState)
    monkeypatch.setattr(judge_mod, "LLMJudge", FakeJudge)
    monkeypatch.setattr(runner_mod, "EvalRunner", FakeRunner)
    monkeypatch.setattr(reporter_mod, "to_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reporter_mod, "to_markdown", lambda *_args, **_kwargs: None)


@pytest.mark.asyncio
async def test_scheduler_wraps_owned_bundle_and_closes_runtime_and_engine_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import audio_graphy.config as config_mod
    import audio_graphy.db as db_mod
    import audio_graphy.services.llm_runtime as runtime_mod
    from audio_graphy.scheduler import run_eval_job

    observed: dict[str, Any] = {}
    _patch_eval_components(
        monkeypatch,
        tmp_path=tmp_path,
        observed=observed,
        runner_error=RuntimeError("evaluation failed"),
    )

    raw_llm = object()
    gateway_llm = object()
    raw_bundle = SimpleNamespace(strong_llm=raw_llm)
    wrapped_bundle = SimpleNamespace(strong_llm=gateway_llm)
    engine = _FakeEngine()
    session_factory = object()
    runtime = _FakeRuntime(wrapped_bundle)
    settings = SimpleNamespace(working_dir=tmp_path)

    monkeypatch.setattr(config_mod, "build_adapters", lambda actual: raw_bundle)
    monkeypatch.setattr(db_mod, "create_db_engine", lambda actual: engine)
    monkeypatch.setattr(db_mod, "create_session_factory", lambda actual: session_factory)

    async def fake_build_runtime(
        actual_settings: object,
        actual_factory: object,
        actual_bundle: object,
    ) -> _FakeRuntime:
        observed["runtime_args"] = (actual_settings, actual_factory, actual_bundle)
        return runtime

    monkeypatch.setattr(runtime_mod, "build_llm_runtime", fake_build_runtime)

    await run_eval_job("run-1", "tenant-a", settings=settings)

    assert observed["runtime_args"] == (settings, session_factory, raw_bundle)
    assert observed["judge_llm"] is gateway_llm
    assert [status for _, status, _ in observed["transitions"]] == ["running", "failed"]
    assert runtime.close_calls == 1
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_scheduler_keeps_injected_bundle_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import audio_graphy.config as config_mod
    import audio_graphy.services.llm_runtime as runtime_mod
    from audio_graphy.scheduler import run_eval_job

    observed: dict[str, Any] = {}
    _patch_eval_components(
        monkeypatch,
        tmp_path=tmp_path,
        observed=observed,
        runner_error=None,
    )

    injected_llm = object()
    injected_bundle = SimpleNamespace(strong_llm=injected_llm)

    def unexpected_build_adapters(_settings: object) -> None:
        raise AssertionError("injected bundle must bypass production adapter construction")

    async def unexpected_build_runtime(*_args: object) -> None:
        raise AssertionError("injected test bundle must remain directly usable")

    monkeypatch.setattr(config_mod, "build_adapters", unexpected_build_adapters)
    monkeypatch.setattr(runtime_mod, "build_llm_runtime", unexpected_build_runtime)

    await run_eval_job(
        "run-2",
        "tenant-a",
        settings=SimpleNamespace(working_dir=tmp_path),
        session_factory=object(),  # type: ignore[arg-type]
        bundle=injected_bundle,  # type: ignore[arg-type]
    )

    assert observed["judge_llm"] is injected_llm
    assert [status for _, status, _ in observed["transitions"]] == ["running", "completed"]


@pytest.mark.parametrize(
    ("runner_error", "expected_code"),
    [(None, 0), (RuntimeError("runner crashed"), 70)],
)
def test_eval_cli_uses_runtime_bundle_and_closes_owned_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_error: Exception | None,
    expected_code: int,
) -> None:
    import audio_graphy.config as config_mod
    import audio_graphy.db as db_mod
    import audio_graphy.eval.judge as judge_mod
    import audio_graphy.eval.reporter as reporter_mod
    import audio_graphy.eval.runner as runner_mod
    import audio_graphy.services.llm_runtime as runtime_mod
    from audio_graphy.eval.cli import main

    observed: dict[str, Any] = {}
    raw_llm = object()
    gateway_llm = object()
    raw_bundle = SimpleNamespace(strong_llm=raw_llm)
    wrapped_bundle = SimpleNamespace(strong_llm=gateway_llm)
    runtime = _FakeRuntime(wrapped_bundle)
    engine = _FakeEngine()
    session_factory = object()
    settings = SimpleNamespace(judge_llm_model_resolved="judge-model")

    class FakeJudge:
        def __init__(self, llm: object) -> None:
            observed["judge_llm"] = llm

    class FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            observed["runner_kwargs"] = kwargs

        async def run(self) -> object:
            if runner_error is not None:
                raise runner_error
            return SimpleNamespace(run_id="eval-1", per_example=())

    async def fake_build_runtime(
        actual_settings: object,
        actual_factory: object,
        actual_bundle: object,
    ) -> _FakeRuntime:
        observed["runtime_args"] = (actual_settings, actual_factory, actual_bundle)
        return runtime

    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(config_mod, "build_adapters", lambda actual: raw_bundle)
    monkeypatch.setattr(db_mod, "create_db_engine", lambda actual: engine)
    monkeypatch.setattr(db_mod, "create_session_factory", lambda actual: session_factory)
    monkeypatch.setattr(runtime_mod, "build_llm_runtime", fake_build_runtime)
    monkeypatch.setattr(judge_mod, "LLMJudge", FakeJudge)
    monkeypatch.setattr(runner_mod, "EvalRunner", FakeRunner)
    monkeypatch.setattr(reporter_mod, "to_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reporter_mod, "to_markdown", lambda *_args, **_kwargs: None)

    gold_path = tmp_path / "gold.yaml"
    gold_path.write_text("[]\n", encoding="utf-8")
    exit_code = main(
        [
            "--gold-set",
            str(gold_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )

    assert exit_code == expected_code
    assert observed["runtime_args"] == (settings, session_factory, raw_bundle)
    assert observed["judge_llm"] is gateway_llm
    assert runtime.close_calls == 1
    assert engine.dispose_calls == 1
