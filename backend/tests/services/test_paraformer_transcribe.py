"""Coverage tests for ``audio_graphy.services.paraformer_service._transcribe_file``.

Pins the timestamp handling of funasr's ``sentence_info``: the entries arrive
untyped, so a sentence whose ``start``/``end`` is missing or non-numeric must be
dropped rather than handed downstream with a fabricated span. The whole-file
fallback segment is only allowed to appear when *no* sentence survived.
"""

from __future__ import annotations

from typing import Any

import pytest

import audio_graphy.services.paraformer_service as svc


class _FakeModel:
    """Stands in for the funasr ``AutoModel`` the service loads at startup."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._result


@pytest.fixture
def stub_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the librosa decode — duration is not what these tests are about."""
    monkeypatch.setattr(svc, "_audio_duration_sec", lambda path: 12.5)


def _install_model(monkeypatch: pytest.MonkeyPatch, result: Any) -> _FakeModel:
    model = _FakeModel(result)
    monkeypatch.setattr(svc, "_MODEL", model)
    return model


@pytest.mark.parametrize(
    "sentence",
    [
        pytest.param({"text": "缺少时间戳"}, id="keys-absent"),
        pytest.param({"start": None, "end": 2000, "text": "缺开始"}, id="start-none"),
        pytest.param({"start": 0, "end": None, "text": "缺结束"}, id="end-none"),
        pytest.param({"start": "abc", "end": 2000, "text": "非数字"}, id="start-not-numeric"),
    ],
)
def test_sentence_without_usable_timestamps_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
    stub_duration: None,
    sentence: dict[str, Any],
) -> None:
    """A missing or non-numeric timestamp drops the sentence, never the request."""
    _install_model(
        monkeypatch,
        [
            {
                "text": "好 的",
                "sentence_info": [
                    sentence,
                    {"start": 3000, "end": 4500, "text": "有 时 间 戳"},
                ],
            }
        ],
    )

    text, segments, duration = svc._transcribe_file("/tmp/a.wav")

    assert text == "好的"
    assert duration == 12.5
    # Only the well-formed sentence survives; the dropped one leaves no hole and
    # no invented span.
    assert segments == [{"id": 1, "start": 3.0, "end": 4.5, "text": "有时间戳"}]


def test_all_sentences_dropped_falls_back_to_whole_file_span(
    monkeypatch: pytest.MonkeyPatch,
    stub_duration: None,
) -> None:
    """With no usable sentence, the caller still gets one real whole-file span."""
    _install_model(
        monkeypatch,
        [{"text": "全 文", "sentence_info": [{"text": "无 时 间 戳"}]}],
    )

    text, segments, duration = svc._transcribe_file("/tmp/a.wav")

    assert text == "全文"
    assert segments == [{"id": 0, "start": 0.0, "end": 12.5, "text": "全文"}]
    assert duration == 12.5


def test_millisecond_timestamps_become_seconds(
    monkeypatch: pytest.MonkeyPatch,
    stub_duration: None,
) -> None:
    """funasr reports milliseconds; the wire schema is seconds."""
    _install_model(
        monkeypatch,
        [
            {
                "text": "一 二",
                "sentence_info": [
                    {"start": 0, "end": 1500, "text": "一"},
                    {"start": 1500, "end": 3250, "text": "二"},
                ],
            }
        ],
    )

    _, segments, _ = svc._transcribe_file("/tmp/a.wav")

    assert [(seg["start"], seg["end"]) for seg in segments] == [(0.0, 1.5), (1.5, 3.25)]


def test_sec_from_ms_reports_unusable_values_as_none() -> None:
    """The conversion helper answers None instead of raising at the call site."""
    assert svc._sec_from_ms(2500) == 2.5
    assert svc._sec_from_ms("2500") == 2.5
    assert svc._sec_from_ms(None) is None
    assert svc._sec_from_ms("abc") is None
    assert svc._sec_from_ms([1]) is None
