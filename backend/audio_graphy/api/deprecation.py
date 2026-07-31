"""Compatibility headers for legacy synchronous tagging APIs.

The old recording-level tag, prompt and dialogue-tag derivation endpoints stay
available during the expand/contract migration.  Responses advertise the
asynchronous governance successor so clients can migrate without guessing.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_API_PREFIX = "/api/v1"
_SUNSET = "Fri, 31 Dec 2027 23:59:59 GMT"


def is_legacy_tagging_path(path: str) -> bool:
    """Return whether ``path`` belongs to the compatibility-only tag surface."""

    if path == f"{_API_PREFIX}/tags" or path.startswith(f"{_API_PREFIX}/tags/"):
        return True
    if path == f"{_API_PREFIX}/prompts" or path.startswith(f"{_API_PREFIX}/prompts/"):
        return True
    return path.startswith(f"{_API_PREFIX}/receptions/") and path.endswith("/dialogue-tags/derive")


class LegacyTaggingDeprecationMiddleware(BaseHTTPMiddleware):
    """Add RFC-style migration metadata without changing endpoint payloads."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        if not is_legacy_tagging_path(request.url.path):
            return response

        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = _SUNSET
        response.headers["Link"] = '</api/v1/tag-jobs>; rel="successor-version"'
        response.headers["Warning"] = (
            '299 AudioGraphy "Legacy tagging API; migrate to versioned tag jobs"'
        )
        return response
