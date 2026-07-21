"""Real adapter exceptions — one base per adapter + subclasses per failure mode.

真实 Adapter 异常体系 —— 每个 adapter 拥有一个 base exception，
按 HTTP 状态码或失败模式派生子类。

Design:
- Each adapter owns a base exception (VADAdapterError / LLMAdapterError / EmbedAdapterError).
- Subclasses correspond to HTTP status codes or semantic failures (timeout / dim mismatch).
- All exceptions carry `url` (redacted by callers via `_redact`) and `status_code` (optional)
  for triage. Adapters RAISE these; callers (pipeline / API) decide retry / surface to user.
- Each exception sets `__module__` so `str(exc.__class__)` reports a stable, importable path
  even when re-exported through `audio_graphy.adapters`.
"""

from __future__ import annotations


def _redact(url: str) -> str:
    """Strip the query string from a URL for safe logging.

    去除 URL 中的 query 字符串，避免在日志中泄漏 token。
    """
    return url.split("?", 1)[0]


class VADAdapterError(Exception):
    """Base for all VAD adapter failures / VAD Adapter 错误基类."""

    __module__ = "audio_graphy.adapters.exceptions"

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class VADRequestError(VADAdapterError):
    """HTTP 400 — audio format unsupported / corrupt WAV."""

    __module__ = "audio_graphy.adapters.exceptions"


class VADTooLargeError(VADAdapterError):
    """HTTP 413 — audio payload exceeds server limit."""

    __module__ = "audio_graphy.adapters.exceptions"


class VADServerError(VADAdapterError):
    """HTTP 5xx — Silero server fault / response shape invalid."""

    __module__ = "audio_graphy.adapters.exceptions"


class VADTimeoutError(VADAdapterError):
    """httpx.TimeoutException or HTTP 504 / 请求超时."""

    __module__ = "audio_graphy.adapters.exceptions"


class LLMAdapterError(Exception):
    """Base for all LLM adapter failures / LLM Adapter 错误基类."""

    __module__ = "audio_graphy.adapters.exceptions"

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.model = model


class LLMBadRequest(LLMAdapterError):
    """HTTP 400 — malformed messages / unsupported model."""

    __module__ = "audio_graphy.adapters.exceptions"


class LLMRateLimitError(LLMAdapterError):
    """HTTP 429 — vLLM rate limit. M4 does NOT retry (PRD §3.2 P1)."""

    __module__ = "audio_graphy.adapters.exceptions"


class LLMServerError(LLMAdapterError):
    """HTTP 5xx — vLLM inference fault / non-JSON response."""

    __module__ = "audio_graphy.adapters.exceptions"


class LLMTimeoutError(LLMAdapterError):
    """httpx.TimeoutException / 请求超时."""

    __module__ = "audio_graphy.adapters.exceptions"


class EmbedAdapterError(Exception):
    """Base for all embedding adapter failures / Embed Adapter 错误基类."""

    __module__ = "audio_graphy.adapters.exceptions"

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class EmbedServerError(EmbedAdapterError):
    """HTTP 5xx — TEI server fault / response shape invalid."""

    __module__ = "audio_graphy.adapters.exceptions"


class EmbedTimeoutError(EmbedAdapterError):
    """httpx.TimeoutException / 请求超时."""

    __module__ = "audio_graphy.adapters.exceptions"


class EmbedDimMismatchError(EmbedAdapterError):
    """Response vector dim != configured `embedding_dim`.

    当 TEI 返回的向量维度与 settings.embedding_dim（通常为 bge-m3 的 1024）不一致时抛出。
    Almost always indicates a TEI `--model-id` misconfiguration.
    """

    __module__ = "audio_graphy.adapters.exceptions"


__all__ = [
    "EmbedAdapterError",
    "EmbedDimMismatchError",
    "EmbedServerError",
    "EmbedTimeoutError",
    "LLMAdapterError",
    "LLMBadRequest",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMTimeoutError",
    "VADAdapterError",
    "VADRequestError",
    "VADServerError",
    "VADTimeoutError",
    "VADTooLargeError",
    "_redact",
]
