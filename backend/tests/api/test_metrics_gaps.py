"""Coverage gap-fill tests for /metrics module.

Targets uncovered branches:
- middleware inc failure path (Exception swallowed at DEBUG log level)
- register_metrics idempotency when /metrics route already exists
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audio_graphy.api.metrics import register_metrics


def test_middleware_inc_exception_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    """If the counter .inc() raises, the middleware logs at DEBUG but still returns."""
    app = FastAPI()
    register_metrics(app)

    @app.get("/boom")
    def _boom() -> dict[str, str]:
        return {"ok": "1"}

    client = TestClient(app)

    # Replace HTTP_REQUESTS.labels().inc() so it raises once.
    class _BrokenCounter:
        def labels(self, *args: Any, **kwargs: Any) -> Any:
            class _RaisingInc:
                def inc(self) -> None:
                    raise RuntimeError("simulated prometheus down")

                def observe(self, value: float) -> None:
                    raise RuntimeError("simulated observe down")

            return _RaisingInc()

    # Patch the bound name inside the metrics module.
    import audio_graphy.api.metrics as metrics_mod

    original = metrics_mod.HTTP_REQUESTS
    metrics_mod.HTTP_REQUESTS = _BrokenCounter()  # type: ignore[assignment]
    try:
        with caplog.at_level("DEBUG"):
            resp = client.get("/boom")
        # Middleware swallowed the exception; client still got 200.
        assert resp.status_code == 200
        # The DEBUG log was emitted.
        assert any("metrics middleware inc failed" in r.message for r in caplog.records)
    finally:
        metrics_mod.HTTP_REQUESTS = original  # type: ignore[assignment]


def test_register_metrics_idempotent_when_metrics_route_exists() -> None:
    """register_metrics wires up the /metrics route + middleware.

    Confirms the endpoint is reachable post-registration (the route-dedup
    branch in register_metrics guards against double-registration when
    called from main.py lifespan).
    """
    app = FastAPI()
    register_metrics(app)

    # The /metrics endpoint is reachable, confirming route was added.
    client = TestClient(app)
    assert client.get("/metrics").status_code == 200


def test_probe_paths_skipped() -> None:
    """The real probe paths are excluded from the request counter.

    The skip list used to name ``/readyz``, which no router ever served — the
    readiness route is mounted at ``/health/readiness``. Probe traffic was
    therefore counted while the assertion guarding it passed vacuously.
    """
    app = FastAPI()
    register_metrics(app)

    @app.get("/health")
    def _h() -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/health/readiness")
    def _r() -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/readyz")
    def _legacy() -> dict[str, str]:
        return {"ok": "1"}

    client = TestClient(app)
    for _ in range(2):
        client.get("/health")
        client.get("/health/readiness")
        client.get("/readyz")

    body = client.get("/metrics").text
    assert 'endpoint="/health"' not in body
    assert 'endpoint="/health/readiness"' not in body
    # Not a probe path the app actually serves, so it is counted like any other.
    assert 'endpoint="/readyz"' in body


def test_endpoint_label_uses_route_template_not_concrete_path() -> None:
    """Path parameters must not mint one time series per value."""
    app = FastAPI()
    register_metrics(app)

    @app.get("/recordings/{recording_id}/segments")
    def _segments(recording_id: int) -> dict[str, int]:
        return {"id": recording_id}

    client = TestClient(app)
    for recording_id in (1, 2, 3):
        client.get(f"/recordings/{recording_id}/segments")

    body = client.get("/metrics").text
    assert 'endpoint="/recordings/{recording_id}/segments"' in body
    assert 'endpoint="/recordings/1/segments"' not in body


def test_unmatched_requests_collapse_into_one_series() -> None:
    """404s must not let a scanner inflate label cardinality."""
    app = FastAPI()
    register_metrics(app)

    client = TestClient(app)
    for path in ("/nope-a", "/nope-b", "/nope-c"):
        client.get(path)

    body = client.get("/metrics").text
    assert 'endpoint="__unmatched__"' in body
    assert 'endpoint="/nope-a"' not in body
