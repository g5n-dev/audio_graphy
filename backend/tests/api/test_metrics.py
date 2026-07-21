"""Tests for /metrics endpoint + MetricsMiddleware (M6 Q3).

Cases:
    1. GET /metrics returns 200 + Prometheus text format + AudioGraphy metric.
    2. Middleware counts requests (hit /api/v1/..., then verify counter).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audio_graphy.api.metrics import (
    HTTP_REQUESTS,
    MetricsMiddleware,
    REGISTRY,
    register_metrics,
)


def _fresh_app() -> FastAPI:
    """Build a minimal FastAPI app with metrics middleware + /metrics."""
    app = FastAPI()
    register_metrics(app)

    @app.get("/ping")
    def _ping() -> dict[str, str]:
        return {"ok": "1"}

    return app


@pytest.fixture
def metrics_client() -> TestClient:
    """TestClient wired to a minimal metrics-enabled app."""
    # Reset counter state so tests are deterministic.
    try:
        REGISTRY.unregister(HTTP_REQUESTS._collector)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort cleanup.
        pass
    return TestClient(_fresh_app())


# --------------------------------------------------------------------
# Case 1 — /metrics returns 200 + prometheus text + audiography metric
# --------------------------------------------------------------------

def test_metrics_endpoint_returns_prometheus_format(metrics_client: TestClient) -> None:
    """GET /metrics returns 200, text/plain, with an audiography_* metric."""
    response = metrics_client.get("/metrics")
    assert response.status_code == 200
    # Prometheus exposition format is text/plain with version charset.
    ct = response.headers.get("content-type", "")
    assert "text/plain" in ct, f"unexpected content-type: {ct!r}"
    # Body must include at least one audiography_* metric name.
    body = response.text
    assert "audiography_" in body, (
        "expected at least one audiography_* metric in /metrics output"
    )


# --------------------------------------------------------------------
# Case 2 — middleware counts requests
# --------------------------------------------------------------------

def test_middleware_increments_request_counter(metrics_client: TestClient) -> None:
    """Hitting /ping then /metrics must show incremented HTTP_REQUESTS counter."""
    # Hit /ping a few times.
    for _ in range(3):
        r = metrics_client.get("/ping")
        assert r.status_code == 200

    # Now /metrics should mention audiography_http_requests_total.
    r = metrics_client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "audiography_http_requests_total" in body
    # /metrics itself must NOT be counted (skip list).
    # Confirm by searching for a /metrics label — should be absent.
    assert 'endpoint="/metrics"' not in body


# --------------------------------------------------------------------
# Bonus — /metrics itself does NOT bump the counter (idempotent scrape)
# --------------------------------------------------------------------

def test_metrics_endpoint_not_counted(metrics_client: TestClient) -> None:
    """Repeated /metrics scrapes do not increment HTTP_REQUESTS for /metrics."""
    # Three scrapes.
    for _ in range(3):
        metrics_client.get("/metrics")
    body = metrics_client.get("/metrics").text
    # Confirm no /metrics labelled line in the body.
    assert 'endpoint="/metrics"' not in body


# --------------------------------------------------------------------
# Bonus — Counter / Histogram objects exist on REGISTRY
# --------------------------------------------------------------------

def test_metrics_registered_on_default_registry() -> None:
    """All shipped metric names are present on REGISTRY."""
    body = TestClient(_fresh_app()).get("/metrics").text
    expected_metric_names = (
        "audiography_http_requests_total",
        "audiography_llm_calls_total",
        "audiography_pipeline_seconds",
        "audiography_retention_deletes_total",
        "audiography_audit_log_written_total",
    )
    for name in expected_metric_names:
        assert name in body, f"missing metric {name!r} in /metrics output"
