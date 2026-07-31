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

from collections.abc import Mapping


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
    """Permanent HTTP 4xx — malformed, unauthorized, missing, or invalid request."""

    __module__ = "audio_graphy.adapters.exceptions"


class LLMTruncatedResponseError(LLMBadRequest):
    """Provider completed billing but stopped generation before a valid result."""

    __module__ = "audio_graphy.adapters.exceptions"

    def __init__(
        self,
        message: str,
        *,
        finish_reason: str,
        usage: Mapping[str, int] | None = None,
        provider_request_id: str | None = None,
        url: str | None = None,
        status_code: int | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(
            message,
            url=url,
            status_code=status_code,
            model=model,
        )
        self.finish_reason = finish_reason
        self.usage = dict(usage or {})
        self.provider_request_id = provider_request_id
        self.billed_usage_known = usage is not None
        self.unknown_billed = usage is None


class LLMRateLimitError(LLMAdapterError):
    """HTTP 429 — provider rate limit; retry policy belongs to ``LLMGateway``."""

    __module__ = "audio_graphy.adapters.exceptions"


class LLMServerError(LLMAdapterError):
    """Retryable transport status (408/425/5xx) or invalid provider response."""

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


class ASRAdapterError(Exception):
    """Base for all ASR adapter failures / ASR Adapter 错误基类."""

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


class ASRRequestError(ASRAdapterError):
    """HTTP 400 / 422 — bad audio / unsupported response_format."""

    __module__ = "audio_graphy.adapters.exceptions"


class ASRAuthError(ASRAdapterError):
    """HTTP 401 / 403 — funASR token rejected (when auth enabled)."""

    __module__ = "audio_graphy.adapters.exceptions"


class ASRTooLargeError(ASRAdapterError):
    """HTTP 413 — audio payload exceeds server limit."""

    __module__ = "audio_graphy.adapters.exceptions"


class ASRRateLimitError(ASRAdapterError):
    """HTTP 429 — funASR rate limit. M5 does NOT retry (PRD §4.4)."""

    __module__ = "audio_graphy.adapters.exceptions"


class ASRServerError(ASRAdapterError):
    """HTTP 5xx — funASR inference fault / non-JSON response."""

    __module__ = "audio_graphy.adapters.exceptions"


class ASRTimeoutError(ASRAdapterError):
    """httpx.TimeoutException / 请求超时."""

    __module__ = "audio_graphy.adapters.exceptions"


# ============================================================
# M7 — Phase 2: CLAP audio embedding + CAM++ voiceprint
# ============================================================


class AudioEmbedAdapterError(Exception):
    """Base for all audio-embedding (CLAP) adapter failures / M7 CLAP 错误基类.

    Carries ``url`` (redacted by callers via ``_redact``) and ``status_code``
    (optional) for triage. Subclasses correspond to HTTP status codes or
    semantic failures (timeout / dim mismatch).
    """

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


class CLAPRequestError(AudioEmbedAdapterError):
    """HTTP 400 / 422 — bad audio format / missing file / unsupported model."""

    __module__ = "audio_graphy.adapters.exceptions"


class CLAPTooLargeError(AudioEmbedAdapterError):
    """HTTP 413 — audio payload exceeds clap-service limit."""

    __module__ = "audio_graphy.adapters.exceptions"


class CLAPTimeoutError(AudioEmbedAdapterError):
    """httpx.TimeoutException or HTTP 504 — clap-service timed out."""

    __module__ = "audio_graphy.adapters.exceptions"


class CLAPServerError(AudioEmbedAdapterError):
    """HTTP 5xx — clap-service inference fault / non-JSON / dim != 512."""

    __module__ = "audio_graphy.adapters.exceptions"


class VoiceprintAdapterError(Exception):
    """Base for all voiceprint (CAM++) adapter failures / M7 CAM++ 错误基类.

    Covers both the diarization endpoint and the voiceprint extraction
    endpoint. Subclasses mirror the ASR / VAD exception mapping matrix.
    """

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


class VoiceprintRequestError(VoiceprintAdapterError):
    """HTTP 400 / 422 — bad audio / missing file / invalid diarize params."""

    __module__ = "audio_graphy.adapters.exceptions"


class VoiceprintTimeoutError(VoiceprintAdapterError):
    """httpx.TimeoutException — diarize / extract_voiceprint timed out."""

    __module__ = "audio_graphy.adapters.exceptions"


class VoiceprintServerError(VoiceprintAdapterError):
    """HTTP 5xx / transport error / malformed JSON / dim != 192 / not L2-normed."""

    __module__ = "audio_graphy.adapters.exceptions"


# ============================================================
# M8 — Phase 4: Streaming VAD / Streaming ASR / WebSocket session
# ============================================================


# Re-exported mix-ins that classify HTTP-style failure modes. M8 follows the
# same pattern as the M4-M7 adapters (per-class base + per-status subclass)
# rather than introducing a parallel taxonomy.
class RequestErrorMixin:
    """Marker mix-in for 4xx-equivalent failures (client-supplied input bad)."""


class ServerErrorMixin:
    """Marker mix-in for 5xx-equivalent failures (upstream service fault)."""


class TimeoutErrorMixin:
    """Marker mix-in for timeout failures (connect / push / drain)."""


class StreamingVADAdapterError(Exception):
    """Base for all streaming VAD adapter failures / M8 流式 VAD 错误基类.

    Covers Silero streaming VAD (``silero_vad.onnx`` local file, 4-state FSM).
    Subclasses mirror the batch VAD exception mapping but apply to per-chunk
    failures rather than per-file failures.
    """

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


class StreamingVADChunkShapeError(StreamingVADAdapterError, RequestErrorMixin):
    """PCM chunk not exactly 1024 bytes (512 samples × 2 bytes).

    L3 locked constraint: 16 kHz mono int16, 512 samples per chunk. Any
    deviation indicates a client bug or transport corruption — fail-fast.
    """

    __module__ = "audio_graphy.adapters.exceptions"


class StreamingVADModelLoadError(StreamingVADAdapterError, ServerErrorMixin):
    """``silero_vad.onnx`` missing or corrupt (ONNX session creation failed).

    Almost always indicates a deployment misconfiguration (model file not
    mounted). Recovery requires re-deploying the model, not retrying.
    """

    __module__ = "audio_graphy.adapters.exceptions"


class StreamingASRAdapterError(Exception):
    """Base for all streaming ASR adapter failures / M8 流式 ASR 错误基类.

    Covers funASR WebSocket:10095 failures (handshake / push / finalize /
    drain). Subclasses map to either HTTP-style categories or to
    WebSocket-specific close codes (1011 internal error, etc.).
    """

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


class StreamingASRRequestError(StreamingASRAdapterError, RequestErrorMixin):
    """funASR WS handshake 400 / 422 — bad init JSON / unsupported model."""

    __module__ = "audio_graphy.adapters.exceptions"


class StreamingASRAuthError(StreamingASRAdapterError, RequestErrorMixin):
    """funASR WS handshake 401 / 403 — token rejected (when auth enabled)."""

    __module__ = "audio_graphy.adapters.exceptions"


class StreamingASRServerError(StreamingASRAdapterError, ServerErrorMixin):
    """funASR WS 1011 internal error / transport error / non-JSON response."""

    __module__ = "audio_graphy.adapters.exceptions"


class StreamingASRConnectTimeout(StreamingASRAdapterError, TimeoutErrorMixin):
    """funASR WS connect timeout (default 5s).

    Triggers fallback to backup funASR replica (R4 mitigation).
    """

    __module__ = "audio_graphy.adapters.exceptions"


class StreamingASRPushTimeout(StreamingASRAdapterError, TimeoutErrorMixin):
    """funASR push timeout — 30s no response on an established connection.

    Indicates the funASR worker is stuck or the GPU is saturated.
    """

    __module__ = "audio_graphy.adapters.exceptions"


class StreamingASRProtocolError(StreamingASRAdapterError, ServerErrorMixin):
    """funASR returned malformed JSON or missing required fields.

    funASR protocol deviations are logged + skipped (the stream continues
    with the next delta); this exception is raised only when the delta
    cannot be safely interpreted at all.
    """

    __module__ = "audio_graphy.adapters.exceptions"


class WebSocketSessionError(Exception):
    """Base for WebSocket session-lifecycle failures / M8 WS 会话错误基类.

    Covers session creation, backpressure, finalization, and tenant
    isolation failures. Distinct from ``StreamingVADAdapterError`` and
    ``StreamingASRAdapterError`` which cover upstream service faults.
    """

    __module__ = "audio_graphy.adapters.exceptions"

    def __init__(
        self,
        message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.session_id = session_id


class WebSocketBackpressureOverflow(WebSocketSessionError):
    """``recv_queue`` exceeded ``MAX_RECV_QUEUE`` (default 200) — force close.

    PRD §5.3 PIPL + R1 mitigation. Triggered when the client pushes PCM
    faster than the server can drain (e.g. ASR push timeout causing
    backlog). Force-close prevents OOM.
    """

    __module__ = "audio_graphy.adapters.exceptions"


class WebSocketSessionStateError(WebSocketSessionError):
    """Session lifecycle violated (e.g. push_pcm called before connect).

    Indicates a programming bug in the caller rather than a network fault.
    """

    __module__ = "audio_graphy.adapters.exceptions"


__all__ = [
    "ASRAdapterError",
    "ASRAuthError",
    "ASRRateLimitError",
    "ASRRequestError",
    "ASRServerError",
    "ASRTimeoutError",
    "ASRTooLargeError",
    "AudioEmbedAdapterError",
    "CLAPRequestError",
    "CLAPServerError",
    "CLAPTimeoutError",
    "CLAPTooLargeError",
    "EmbedAdapterError",
    "EmbedDimMismatchError",
    "EmbedServerError",
    "EmbedTimeoutError",
    "LLMAdapterError",
    "LLMBadRequest",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMTimeoutError",
    "LLMTruncatedResponseError",
    "RequestErrorMixin",
    "ServerErrorMixin",
    "StreamingASRAdapterError",
    "StreamingASRAuthError",
    "StreamingASRConnectTimeout",
    "StreamingASRProtocolError",
    "StreamingASRPushTimeout",
    "StreamingASRRequestError",
    "StreamingASRServerError",
    "StreamingVADAdapterError",
    "StreamingVADChunkShapeError",
    "StreamingVADModelLoadError",
    "TimeoutErrorMixin",
    "VADAdapterError",
    "VADRequestError",
    "VADServerError",
    "VADTimeoutError",
    "VADTooLargeError",
    "VoiceprintAdapterError",
    "VoiceprintRequestError",
    "VoiceprintServerError",
    "VoiceprintTimeoutError",
    "WebSocketBackpressureOverflow",
    "WebSocketSessionError",
    "WebSocketSessionStateError",
    "_redact",
]
