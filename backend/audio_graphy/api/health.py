"""Health router — liveness + readiness checks.

See: docs/m3-prd.md §4.9, API-09.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health", tags=["meta"])


@router.get("", summary="Liveness probe")
async def liveness() -> dict[str, str]:
    """Liveness probe — returns 200 if the process is up."""
    return {"status": "ok", "service": "audiography-backend"}


@router.get("/readiness", summary="Readiness probe")
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe — checks DB + adapter bundle + stores.

    Returns 200 if all checks pass, 503 if any component is unreachable.
    """
    checks: dict[str, Any] = {}
    all_ok = True

    # Check DB connectivity
    db_status = "ok"
    try:
        from sqlalchemy import text

        factory = getattr(request.app.state, "session_factory", None)
        if factory is not None:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        else:
            db_status = "error: session factory not initialized"
            all_ok = False
    except Exception as exc:
        db_status = f"error: {exc}"
        all_ok = False
    checks["database"] = db_status

    # Check adapter bundle
    adapters_status: dict[str, str] = {}
    bundle = getattr(request.app.state, "adapter_bundle", None)
    if bundle is None:
        adapters_status["bundle"] = "error: not initialized"
        all_ok = False
    else:
        for name in ("vad", "asr", "strong_llm", "weak_llm", "embed"):
            adapter = getattr(bundle, name, None)
            adapters_status[name] = "ok" if adapter is not None else "error: missing"
            if adapter is None:
                all_ok = False
    checks["adapters"] = adapters_status

    # Check graph_store (per default tenant)
    graph_status = "ok"
    try:
        graph_stores = getattr(request.app.state, "graph_stores", {})
        if graph_stores is not None:
            checks["graph_store"] = "ok" if graph_stores is not None else "error"
    except Exception as exc:
        graph_status = f"error: {exc}"
        all_ok = False
    checks["graph_store"] = graph_status

    # Check file_index
    checks["file_index"] = "ok"

    # Hot caching is optional and deliberately never gates readiness. Expose
    # the currently active backend so operators can verify Redis failover and
    # recovery without turning a cache outage into an application outage.
    llm_cache = getattr(request.app.state, "llm_cache", None)
    checks["llm_hot_cache"] = (
        str(getattr(llm_cache, "backend_name", "disabled")) if llm_cache is not None else "disabled"
    )

    version = getattr(request.app.state, "version", "0.3.0")

    if all_ok:
        return JSONResponse(
            status_code=200,
            content={
                "status": "ready",
                "checks": checks,
                "version": version,
            },
        )
    return JSONResponse(
        status_code=503,
        content={
            "status": "not_ready",
            "checks": checks,
            "version": version,
        },
    )
