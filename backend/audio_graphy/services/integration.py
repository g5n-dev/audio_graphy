"""Open-API integration service: credentials, uploads, and result callbacks.

The machine-to-machine surface has exactly three verbs — upload a recording,
query its computation status, get told when it is done — and this module owns
everything behind them that is not already the ingestion pipeline:

* API-key lifecycle. Only the SHA-256 of a key is stored; the plaintext
  (``agk_…``) exists in one HTTP response and nowhere else.
* Webhook signing secrets are *derived*, not stored:
  ``HMAC(signing_root, "integration-webhook:v1:<key id>")``. A database backup
  therefore contains nothing that signs callbacks.
* Callback enqueueing runs inside the SAME transaction as the recording's
  terminal status write (see ``services/indexing.py``), cloning the
  ``erasure_outbox`` recipe: completion and notification cannot diverge
  across a crash.
* Delivery is a leased outbox loop with exponential backoff and a
  ``dead_letter`` end state — an unreachable receiver becomes visible, not
  silently dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.integration import IntegrationCallback, IntegrationUpload

logger = logging.getLogger(__name__)

_KEY_PREFIX = "agk_"
#: Retry schedule in seconds; after the last slot the row goes to dead_letter.
_BACKOFF_SEC = (60, 300, 1_800, 7_200, 21_600)
_UUID_NS = uuid.UUID("6f7a2f6e-9d1b-4bb4-8a63-1d1de4c1a001")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str]:
    """Return ``(plaintext, sha256_hex)``. The plaintext is shown once."""

    plaintext = _KEY_PREFIX + secrets.token_hex(20)
    return plaintext, hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def derive_webhook_secret(signing_root: bytes, api_key_id: int) -> str:
    """Per-key callback signing secret, derived rather than stored.

    Derivation keys the secret to the master key AND the row id: rotating the
    master key rotates every webhook secret (documented), and no secret
    material travels with a database backup.
    """

    return hmac.new(
        signing_root, f"integration-webhook:v1:{api_key_id}".encode(), hashlib.sha256
    ).hexdigest()


def load_signing_root(master_key_path: str, jwt_secret: str) -> bytes:
    """The key material webhook secrets derive from.

    The master key file is authoritative. Development setups without one fall
    back to the JWT secret so the open API stays usable — acceptable there
    because dev signatures protect nothing; production compose always
    provisions the master key (``master-key-init``).
    """

    try:
        material = Path(master_key_path).read_bytes()
        if len(material) >= 16:
            return material
        logger.warning(
            "master key at %s is shorter than 16 bytes; using JWT secret", master_key_path
        )
    except OSError:
        logger.warning(
            "master key unreadable at %s; webhook secrets derive from JWT secret", master_key_path
        )
    return jwt_secret.encode("utf-8")


def sign_callback(secret: str, timestamp: int, body: bytes) -> str:
    """Stripe-style detached signature: HMAC over ``"<t>." + body``.

    The timestamp inside the MAC is what lets receivers reject replays without
    keeping a nonce store.
    """

    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


# ---------------------------------------------------------------------------
# Callback-target validation (SSRF)
# ---------------------------------------------------------------------------


def validate_callback_url(url: str, *, allow_private: bool) -> None:
    """Refuse callback targets that would turn delivery into an SSRF probe.

    Blocks non-HTTP schemes and — unless the deployment explicitly opts in for
    an internal network — loopback, private, link-local and metadata ranges.
    The check re-runs at delivery time too, so a DNS record that changed after
    registration does not get a free pass.

    Residual risk stated plainly: a hostname can re-resolve between this check
    and the connect. Deployments that need a hard guarantee enforce it at
    egress; ``allow_private`` exists because 私有化 targets usually ARE on
    private ranges.
    """

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("callback_url must be an http(s) URL with a host")
    if allow_private:
        return
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, None)
        addresses = [info[4][0] for info in infos]
    except OSError:
        # Unresolvable now may resolve at delivery; delivery re-validates.
        return
    for raw in addresses:
        # getaddrinfo's sockaddr host is typed str|int (AF_* dependent); ours
        # are always AF_INET/AF_INET6 strings, possibly with a %zone suffix.
        address = ipaddress.ip_address(str(raw).split("%")[0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValueError(
                f"callback_url resolves to a non-public address ({raw}); "
                "set INTEGRATION_ALLOW_PRIVATE_CALLBACK_URLS=true only on an "
                "isolated internal network"
            )


# ---------------------------------------------------------------------------
# Enqueue — called from the pipeline's terminal transitions, same transaction
# ---------------------------------------------------------------------------


def _event_id(upload_id: int, generation: int, status: str) -> str:
    """Deterministic id: one callback per (upload, generation, terminal status).

    A retry of the same generation that fails again therefore dedupes on the
    unique index instead of spamming the receiver, while an operator-forced
    rerun (new generation) legitimately notifies again.
    """

    return str(uuid.uuid5(_UUID_NS, f"{upload_id}:{generation}:{status}"))


async def enqueue_recording_callbacks(
    session: AsyncSession,
    *,
    tenant_id: str,
    recording_id: int,
    generation: int,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    """Insert callback intents for every upload watching this recording.

    MUST be called inside the transaction that writes the terminal status —
    that co-commit is the entire crash-consistency story. Returns the number
    of rows enqueued (0 when the recording did not arrive via the open API or
    the caller registered no callback_url).
    """

    uploads = (
        (
            await session.execute(
                select(IntegrationUpload).where(
                    IntegrationUpload.tenant_id == tenant_id,
                    IntegrationUpload.recording_id == recording_id,
                    IntegrationUpload.callback_url.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    enqueued = 0
    for upload in uploads:
        payload: dict[str, Any] = {
            "event_id": _event_id(upload.id, generation, status),
            "event_type": f"recording.{status}",
            "external_ref": upload.external_ref,
            "recording_id": recording_id,
            "status": status,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        if error_code or error_message:
            payload["error"] = {"code": error_code, "message": error_message}
        # A nested transaction per row: one duplicate (retry of the same
        # generation) must not poison the outer terminal-status transaction.
        try:
            async with session.begin_nested():
                session.add(
                    IntegrationCallback(
                        tenant_id=tenant_id,
                        upload_id=upload.id,
                        event_id=payload["event_id"],
                        callback_url=str(upload.callback_url),
                        payload=payload,
                    )
                )
                await session.flush()
            enqueued += 1
        except IntegrityError:
            continue
    return enqueued


# ---------------------------------------------------------------------------
# Delivery worker
# ---------------------------------------------------------------------------


class IntegrationCallbackWorker:
    """Leased outbox loop delivering signed callbacks with backoff.

    Single instance per process is assumed by compose today; the lease columns
    make a second instance safe anyway, the same way ``erasure_outbox`` and
    ``tag_worker`` handle it.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        signing_root: bytes,
        allow_private_targets: bool,
        worker_id: str,
        poll_interval_sec: float = 2.0,
        request_timeout_sec: float = 10.0,
        lease_sec: int = 120,
        batch_size: int = 10,
    ) -> None:
        self._factory = session_factory
        self._signing_root = signing_root
        self._allow_private = allow_private_targets
        self._worker_id = worker_id
        self._poll_interval = poll_interval_sec
        self._timeout = request_timeout_sec
        self._lease_sec = lease_sec
        self._batch = batch_size
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                delivered = await self.run_once()
            except Exception:
                logger.exception("integration callback sweep failed")
                delivered = 0
            if delivered == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval)

    async def run_once(self) -> int:
        """Claim one batch, deliver each row, return how many were attempted."""

        claimed = await self._claim_batch()
        for callback_id in claimed:
            await self._deliver(callback_id)
        return len(claimed)

    async def _claim_batch(self) -> list[int]:
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            rows = (
                (
                    await session.execute(
                        select(IntegrationCallback)
                        .where(
                            IntegrationCallback.status.in_(("pending", "failed")),
                            IntegrationCallback.available_at <= now,
                        )
                        .order_by(IntegrationCallback.available_at)
                        .limit(self._batch)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            claimed: list[int] = []
            for row in rows:
                if row.lease_expires_at is not None and row.lease_expires_at > now:
                    continue
                row.status = "processing"
                row.lease_owner = self._worker_id
                row.lease_expires_at = now + timedelta(seconds=self._lease_sec)
                claimed.append(row.id)
            return claimed

    async def _deliver(self, callback_id: int) -> None:
        async with self._factory() as session, session.begin():
            row = await session.get(IntegrationCallback, callback_id, with_for_update=True)
            if row is None or row.status != "processing":
                return
            if row.lease_owner != self._worker_id:
                return

            api_key_id = (
                await session.execute(
                    select(IntegrationUpload.api_key_id).where(
                        IntegrationUpload.id == row.upload_id
                    )
                )
            ).scalar_one_or_none()

            error = await self._post(row, api_key_id)
            now = datetime.now(UTC)
            row.lease_owner = None
            row.lease_expires_at = None
            if error is None:
                row.status = "succeeded"
                row.completed_at = now
                row.last_error = None
                return
            row.attempts += 1
            row.last_error = error[:2000]
            if row.attempts >= len(_BACKOFF_SEC):
                row.status = "dead_letter"
                logger.error(
                    "integration callback %s dead-lettered after %d attempts: %s",
                    row.event_id,
                    row.attempts,
                    error,
                )
            else:
                row.status = "failed"
                row.available_at = now + timedelta(seconds=_BACKOFF_SEC[row.attempts - 1])

    async def _post(self, row: IntegrationCallback, api_key_id: int | None) -> str | None:
        """One delivery attempt. Returns None on 2xx, an error string otherwise."""

        try:
            validate_callback_url(row.callback_url, allow_private=self._allow_private)
        except ValueError as exc:
            return f"target rejected: {exc}"

        body = json.dumps(row.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-AudioGraphy-Event": str(row.payload.get("event_type", "")),
            "X-AudioGraphy-Delivery": row.event_id,
        }
        if api_key_id is not None:
            secret = derive_webhook_secret(self._signing_root, api_key_id)
            headers["X-AudioGraphy-Signature"] = sign_callback(secret, int(time.time()), body)

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(row.callback_url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return f"transport: {exc}"
        if 200 <= response.status_code < 300:
            return None
        return f"HTTP {response.status_code}: {response.text[:200]}"
