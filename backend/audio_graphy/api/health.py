"""Health router — liveness + readiness checks.

See: docs/m3-prd.md §4.9, API-09.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def _expected_head() -> str | None:
    """The revision no other migration supersedes. Blocking; callers thread it out.

    Parsed from the files rather than through ``alembic.script``: the repository
    ships its own ``backend/alembic/`` package, which shadows the installed
    ``alembic`` distribution whenever the process runs from ``backend/`` — and
    that is exactly how the container runs. Importing it here would make this
    probe raise ModuleNotFoundError and report the schema as unreadable.

    Returns None if the versions directory is missing or has more than one head;
    the caller treats that as "cannot tell", not as a failure.
    """

    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    if not versions.is_dir():
        return None
    revisions: set[str] = set()
    parents: set[str] = set()
    for script in versions.glob("*.py"):
        text_content = script.read_text(encoding="utf-8")
        revision = re.search(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)', text_content, re.M)
        down = re.search(r'^down_revision(?::[^=]*)?\s*=\s*["\']([^"\']+)', text_content, re.M)
        if revision:
            revisions.add(revision.group(1))
        if down:
            parents.add(down.group(1))
    heads = revisions - parents
    return heads.pop() if len(heads) == 1 else None


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

    # The schema must actually be at head, not merely reachable.
    #
    # `alembic upgrade head` runs in the container command before uvicorn, and a
    # clean-clone acceptance caught it no-opping against an empty database: the
    # process started, every probe passed, and every request answered "table
    # doesn't exist" — including login. Liveness cannot see that and neither
    # could readiness, so the deployment looked correct while serving nothing.
    migrations_status = "ok"
    try:
        from sqlalchemy import text

        factory = getattr(request.app.state, "session_factory", None)
        if factory is None:
            migrations_status = "unknown: session factory not initialized"
            all_ok = False
        else:
            async with factory() as session:
                bind = session.bind
                dialect = bind.dialect.name if bind is not None else "unknown"
                if dialect != "mysql":
                    # Test and dev databases build their schema with create_all and
                    # have no alembic_version. Reporting that as a failure would make
                    # the probe fail everywhere it is exercised, which is how a check
                    # stops being read.
                    migrations_status = f"skipped: {dialect} is not migrated"
                    applied = None
                else:
                    result = await session.execute(text("SELECT version_num FROM alembic_version"))
                    applied = result.scalar_one_or_none()
                    expected = await asyncio.to_thread(_expected_head)
                    if applied is None:
                        migrations_status = "error: schema has never been migrated"
                        all_ok = False
                    elif expected is not None and applied != expected:
                        migrations_status = f"error: at {applied}, expected {expected}"
                        all_ok = False
    except Exception as exc:
        # A missing alembic_version table raises here, which is the same failure
        # as "never migrated" and must not read as a probe malfunction.
        migrations_status = f"error: {exc}"
        all_ok = False
    checks["migrations"] = migrations_status

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

    # The graph store is loaded lazily per tenant, so an empty cache is normal.
    # What must exist is the factory that loads it — its absence means the
    # lifespan never got that far.
    if getattr(request.app.state, "graph_store_factory", None) is None:
        checks["graph_store"] = "error: factory not initialized"
        all_ok = False
    else:
        checks["graph_store"] = "ok"

    # Graph and file indexes are written to working_dir; unreadable or
    # unwritable storage is invisible until the first ingestion fails.
    working_dir = getattr(getattr(request.app.state, "settings", None), "working_dir", None)
    if working_dir is None:
        checks["working_dir"] = "error: not configured"
        all_ok = False
    elif not os.access(str(working_dir), os.W_OK):
        checks["working_dir"] = f"error: not writable: {working_dir}"
        all_ok = False
    else:
        checks["working_dir"] = "ok"

    # Subsystems whose wiring failed during lifespan. The process stays up so it
    # can be inspected, but it must not be handed traffic as if it were whole.
    degradations = getattr(request.app.state, "startup_degradations", []) or []
    if degradations:
        checks["startup"] = {item["component"]: item["error"] for item in degradations}
        all_ok = False
    else:
        checks["startup"] = "ok"

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
