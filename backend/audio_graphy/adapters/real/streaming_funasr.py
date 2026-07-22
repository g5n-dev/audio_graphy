"""Streaming funASR adapter — WebSocket:10095 client with per-tenant pool.

M8 Phase 4 (WS-1 / T3). Streaming counterpart to ``adapters/real/funasr.py``
(which calls the HTTP OpenAI-compatible endpoint over a batch API).

funASR streaming protocol (PRD Appendix A):
    - Init JSON: ``{"mode": "2pass", "chunk_size": [5,10,5], ...}``.
    - Audio push: binary PCM (little-endian float32, 8192 bytes/chunk = 2048 samples).
    - Drain: send ``{"is_speaking": false}``, await ``is_final`` deltas.

Two output flavours:
    - ``2pass-online`` → ``ASRDeltaResult.mode="realtime"`` (frontend-only, not入图).
    - ``2pass-offline`` + ``is_final=true`` → ``ASRDeltaResult.mode="confirmed"`` (入图).

Exception mapping mirrors batch ``funasr.py`` but for WebSocket semantics
(1011 close / connect timeout / push timeout / malformed JSON).

The ``websockets`` library is imported lazily so deployments that only run
mock-mode tests don't require it. Tests inject a fake ``ws_client`` via the
constructor to avoid real network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from audio_graphy.adapters.exceptions import (
    StreamingASRAuthError,
    StreamingASRConnectTimeout,
    StreamingASRProtocolError,
    StreamingASRPushTimeout,
    StreamingASRRequestError,
    StreamingASRServerError,
    _redact,
)
from audio_graphy.adapters.protocols import (
    ASRDeltaResult,
    StreamingASRAdapter,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# L2 locked — funASR streaming protocol constants.
_DEFAULT_MODEL = "paraformer-zh-streaming"
_DEFAULT_CHUNK_SIZE: tuple[int, int, int] = (5, 10, 5)
_DEFAULT_CHUNK_INTERVAL = 10
_DEFAULT_CONNECT_TIMEOUT_SEC = 5.0
_DEFAULT_PUSH_TIMEOUT_SEC = 30.0
_DEFAULT_FINALIZE_TIMEOUT_SEC = 5.0

# funASR accepts int16 PCM as well; we send int16 (matches VAD format)
# to avoid an extra conversion step on the server side.
_PCM_FORMAT = "pcm"


class StreamingFunASRAdapter:
    """Real streaming ASR backed by funASR WebSocket:10095.

    Lifecycle:
        - ``connect()`` opens WS, sends init JSON, awaits server readiness.
        - ``push_pcm()`` sends binary + awaits next JSON delta.
        - ``finalize()`` sends ``{"is_speaking": false}``, drains until ``is_final``.
        - ``aclose()`` closes WS (idempotent).

    Connection pool:
        - Owned by ``FunASRConnectionPool`` (Q1 per-tenant pool).
        - Single adapter instance is bound to one session.

    Args:
        ws_url: funASR server URL, e.g. ``"ws://funasr:10095"``.
        model: ``paraformer-zh-streaming`` (L2 locked).
        chunk_size: ``[5, 10, 5]`` (L2 locked — 600ms lookahead).
        chunk_interval: 10 (funASR default).
        connect_timeout_sec: Handshake timeout (default 5s).
        push_timeout_sec: Per-push response timeout (default 30s).
        finalize_timeout_sec: Drain timeout at finalize (default 5s).
        tenant_id: Tenant scope (for logging; pool isolation is enforced
            by ``FunASRConnectionPool``).
        ws_client: Optional pre-connected client (testing hook). When None,
            ``connect()`` creates one via ``websockets.connect()``.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        model: str = _DEFAULT_MODEL,
        chunk_size: tuple[int, int, int] = _DEFAULT_CHUNK_SIZE,
        chunk_interval: int = _DEFAULT_CHUNK_INTERVAL,
        connect_timeout_sec: float = _DEFAULT_CONNECT_TIMEOUT_SEC,
        push_timeout_sec: float = _DEFAULT_PUSH_TIMEOUT_SEC,
        finalize_timeout_sec: float = _DEFAULT_FINALIZE_TIMEOUT_SEC,
        tenant_id: str = "default",
        ws_client: Any = None,
    ) -> None:
        self._ws_url = ws_url.rstrip("/")
        self._model = model
        self._chunk_size = chunk_size
        self._chunk_interval = chunk_interval
        self._connect_timeout = connect_timeout_sec
        self._push_timeout = push_timeout_sec
        self._finalize_timeout = finalize_timeout_sec
        self._tenant_id = tenant_id

        self._ws: Any = ws_client
        self._owns_ws = ws_client is None  # if injected, the caller owns lifecycle
        self._session_id: str | None = None
        self._hotwords: Sequence[str] = ()
        self._closed: bool = False

    # --------------------------------------------------------------
    # Protocol methods
    # --------------------------------------------------------------
    async def connect(
        self,
        *,
        session_id: str,
        tenant_id: str,
        hotwords: Sequence[str] = (),
    ) -> None:
        """Open funASR WS and send init JSON.

        Raises:
            StreamingASRConnectTimeout: handshake didn't complete in time.
            StreamingASRRequestError: 400 / 422 from funASR.
            StreamingASRAuthError: 401 / 403 from funASR.
            StreamingASRServerError: WS 1011 / transport error.
        """
        self._session_id = session_id
        self._hotwords = tuple(hotwords)
        # Don't overwrite tenant_id if explicitly provided later via pool;
        # but if our ctor tenant was default, adopt the per-session value.
        if self._tenant_id == "default":
            self._tenant_id = tenant_id

        if self._ws is None:
            await self._open_ws()

        init_payload = self._build_init_payload(session_id)
        try:
            await self._ws.send(json.dumps(init_payload))
        except Exception as exc:
            logger.warning(
                "funASR init send failed url=%s session=%s err=%s",
                _redact(self._ws_url), session_id, exc,
            )
            raise StreamingASRServerError(
                f"funASR init send failed: {exc}",
                url=_redact(self._ws_url),
            ) from exc

        logger.debug(
            "funASR connect ok url=%s session=%s tenant=%s hotwords=%d",
            _redact(self._ws_url), session_id, self._tenant_id, len(self._hotwords),
        )

    async def push_pcm(self, pcm: bytes, *, seq: int) -> ASRDeltaResult:
        """Send one PCM chunk and await the next delta.

        Raises:
            StreamingASRPushTimeout: no response within ``push_timeout_sec``.
            StreamingASRProtocolError: malformed JSON from funASR.
            StreamingASRServerError: WS closed mid-push.
        """
        if self._ws is None:
            raise StreamingASRServerError(
                "push_pcm called before connect()",
                url=_redact(self._ws_url),
            )

        try:
            await self._ws.send(pcm)
        except Exception as exc:
            logger.warning(
                "funASR push send failed url=%s seq=%d err=%s",
                _redact(self._ws_url), seq, exc,
            )
            raise StreamingASRServerError(
                f"funASR push send failed: {exc}",
                url=_redact(self._ws_url),
            ) from exc

        raw = await self._recv_json(timeout_sec=self._push_timeout, seq=seq)
        return self._map_delta(raw, seq)

    async def finalize(self) -> tuple[ASRDeltaResult, ...]:
        """Send ``is_speaking=false`` and drain trailing deltas.

        Returns:
            Tuple of 0..N ASRDeltaResult. Typically 1-2 trailing confirmed
            deltas remain after the marker is sent.

        Raises:
            StreamingASRPushTimeout: drain exceeds ``finalize_timeout_sec``.
        """
        if self._ws is None:
            return ()

        try:
            await self._ws.send(json.dumps({"is_speaking": False}))
        except Exception as exc:
            logger.warning(
                "funASR finalize send failed url=%s err=%s",
                _redact(self._ws_url), exc,
            )
            return ()

        out: list[ASRDeltaResult] = []
        deadline = asyncio.get_event_loop().time() + self._finalize_timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(
                    self._ws.recv(), timeout=remaining,
                )
            except TimeoutError:
                break
            except Exception as exc:
                logger.debug("funASR finalize drain ended: %s", exc)
                break
            delta = self._safe_map_delta(raw, seq=-1)
            if delta is None:
                continue
            out.append(delta)
            if delta.mode == "confirmed" and delta.is_final:
                break
        return tuple(out)

    async def aclose(self) -> None:
        """Close the WebSocket. Idempotent."""
        if self._ws is None or not self._owns_ws:
            self._closed = True
            return
        if self._closed:
            return
        try:
            await self._ws.close()
        except Exception as exc:
            logger.debug("funASR aclose error (ignored): %s", exc)
        finally:
            self._closed = True
            self._ws = None

    # --------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------
    def _build_init_payload(self, session_id: str) -> dict[str, Any]:
        """Build the funASR init JSON.

        hotwords are mapped to the ``"{\"alias\":1}"`` format funASR expects.
        """
        hotwords_blob: dict[str, int] = dict.fromkeys(self._hotwords, 1)
        return {
            "mode": "2pass",
            "chunk_size": list(self._chunk_size),
            "chunk_interval": self._chunk_interval,
            "wav_name": f"session_{session_id}",
            "wav_format": _PCM_FORMAT,
            "is_speaking": True,
            "hotwords": json.dumps(hotwords_blob, ensure_ascii=False),
            "itn": True,
            "audio_fs": 16000,
        }

    async def _open_ws(self) -> None:
        """Lazy-import websockets and open a connection."""
        try:
            import websockets
        except ImportError as exc:
            raise StreamingASRServerError(
                f"websockets package not installed: {exc}",
                url=_redact(self._ws_url),
            ) from exc

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self._ws_url, max_size=None),
                timeout=self._connect_timeout,
            )
        except TimeoutError as exc:
            raise StreamingASRConnectTimeout(
                f"funASR connect timeout after {self._connect_timeout}s",
                url=_redact(self._ws_url),
            ) from exc
        except Exception as exc:
            msg = str(exc).lower()
            if "401" in msg or "403" in msg:
                raise StreamingASRAuthError(
                    f"funASR auth rejected: {exc}",
                    url=_redact(self._ws_url),
                ) from exc
            if "400" in msg or "422" in msg:
                raise StreamingASRRequestError(
                    f"funASR bad request: {exc}",
                    url=_redact(self._ws_url),
                ) from exc
            raise StreamingASRServerError(
                f"funASR WS open failed: {exc}",
                url=_redact(self._ws_url),
            ) from exc

    async def _recv_json(self, *, timeout_sec: float, seq: int) -> dict[str, Any]:
        """Receive one JSON message from funASR.

        Raises:
            StreamingASRPushTimeout: no message in time.
            StreamingASRProtocolError: malformed JSON.
            StreamingASRServerError: WS closed.
        """
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout_sec)
        except TimeoutError as exc:
            raise StreamingASRPushTimeout(
                f"funASR push timeout seq={seq} after {timeout_sec}s",
                url=_redact(self._ws_url),
            ) from exc
        except Exception as exc:
            raise StreamingASRServerError(
                f"funASR WS recv failed: {exc}",
                url=_redact(self._ws_url),
            ) from exc

        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise StreamingASRProtocolError(
                    f"funASR returned non-UTF8 binary: {exc}",
                    url=_redact(self._ws_url),
                ) from exc

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise StreamingASRProtocolError(
                f"funASR returned malformed JSON: {exc}",
                url=_redact(self._ws_url),
            ) from exc

        if not isinstance(payload, dict):
            raise StreamingASRProtocolError(
                f"funASR JSON is not an object: {raw[:120]!r}",
                url=_redact(self._ws_url),
            )
        return payload

    def _safe_map_delta(self, raw: Any, *, seq: int) -> ASRDeltaResult | None:
        """Best-effort delta mapping for finalize drain (swallows protocol errors)."""
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.debug("funASR finalize drain non-UTF8 frame, skipping")
                return None
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return self._map_delta(payload, seq)
        except StreamingASRProtocolError:
            return None

    def _map_delta(self, payload: dict[str, Any], seq: int) -> ASRDeltaResult:
        """Map a funASR delta dict to ASRDeltaResult.

        funASR ``mode`` field:
            - ``"2pass-online"`` → realtime delta.
            - ``"2pass-offline"`` → confirmed delta (sentence-final).
        """
        mode_raw = str(payload.get("mode", ""))
        if mode_raw == "2pass-online":
            mode = "realtime"
            is_final = False
        elif mode_raw == "2pass-offline":
            mode = "confirmed"
            # funASR sets is_final only on the offline sentence-final delta;
            # absent → assume True for offline (funASR 1.0.5 quirk).
            is_final = bool(payload.get("is_final", True))
        else:
            # Unknown mode → log + treat as realtime (safer default).
            logger.debug("funASR unknown mode=%s, falling back to realtime", mode_raw)
            mode = "realtime"
            is_final = False

        text = str(payload.get("text", ""))
        sentence_id = int(payload.get("sentence_id", 0) or 0)
        confidence = float(payload.get("confidence", 0.95) or 0.95)

        return ASRDeltaResult(
            seq=seq,
            mode=mode,
            text=text,
            is_final=is_final,
            sentence_id=sentence_id,
            confidence=confidence,
        )


# Protocol satisfaction check (fails at import if drift).
_STREAMING_ASR_PROTOCOL_CHECK: StreamingASRAdapter = StreamingFunASRAdapter(
    ws_url="ws://example",
)
