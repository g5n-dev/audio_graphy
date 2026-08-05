"""Result callbacks: enqueue semantics, signed delivery, backoff, dead letter.

The enqueue tests run against the same session the terminal status writes use,
because co-commit IS the feature; the delivery tests mock the receiver with
respx and verify the signature the way an external system would.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.integration import ApiKey, IntegrationCallback, IntegrationUpload
from audio_graphy.models.recording import Recording
from audio_graphy.services.integration import (
    IntegrationCallbackWorker,
    derive_webhook_secret,
    enqueue_recording_callbacks,
    generate_api_key,
    validate_callback_url,
)

_TENANT = "chang_an"


async def _seed_upload(
    session_factory: async_sessionmaker[AsyncSession],
    recording_id: int,
    *,
    callback_url: str | None = "https://receiver.example/hook",
) -> int:
    async with session_factory() as session, session.begin():
        _plaintext, key_hash = generate_api_key()
        key = ApiKey(tenant_id=_TENANT, name="cb-test", key_hash=key_hash, created_by=1)
        session.add(key)
        await session.flush()
        upload = IntegrationUpload(
            tenant_id=_TENANT,
            external_ref="crm-cb-1",
            recording_id=recording_id,
            api_key_id=key.id,
            callback_url=callback_url,
        )
        session.add(upload)
        await session.flush()
        return int(upload.id)


class TestEnqueue:
    @pytest.mark.asyncio
    async def test_one_callback_per_generation_and_status(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_recording: Recording,
    ) -> None:
        """A retried attempt of the same generation must not notify twice."""

        await _seed_upload(session_factory, seeded_recording.id)
        async with session_factory() as session, session.begin():
            first = await enqueue_recording_callbacks(
                session,
                tenant_id=_TENANT,
                recording_id=seeded_recording.id,
                generation=1,
                status="failed",
                error_code="RuntimeError",
                error_message="ASR unavailable",
            )
            duplicate = await enqueue_recording_callbacks(
                session,
                tenant_id=_TENANT,
                recording_id=seeded_recording.id,
                generation=1,
                status="failed",
            )
            rerun = await enqueue_recording_callbacks(
                session,
                tenant_id=_TENANT,
                recording_id=seeded_recording.id,
                generation=2,
                status="indexed",
            )
        assert (first, duplicate, rerun) == (1, 0, 1)

        async with session_factory() as session:
            rows = (await session.execute(select(IntegrationCallback))).scalars().all()
        assert len(rows) == 2
        failed = next(row for row in rows if row.payload["status"] == "failed")
        assert failed.payload["event_type"] == "recording.failed"
        assert failed.payload["external_ref"] == "crm-cb-1"
        assert failed.payload["error"]["code"] == "RuntimeError"
        # Content discipline: ids and states, never transcript or paths.
        assert "path" not in failed.payload

    @pytest.mark.asyncio
    async def test_a_recording_without_callback_url_enqueues_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_recording: Recording,
    ) -> None:
        await _seed_upload(session_factory, seeded_recording.id, callback_url=None)
        async with session_factory() as session, session.begin():
            enqueued = await enqueue_recording_callbacks(
                session,
                tenant_id=_TENANT,
                recording_id=seeded_recording.id,
                generation=1,
                status="indexed",
            )
        assert enqueued == 0


class TestDelivery:
    def _worker(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> IntegrationCallbackWorker:
        return IntegrationCallbackWorker(
            session_factory,
            signing_root=b"unit-test-signing-root",
            allow_private_targets=True,  # receiver.example resolves nowhere in CI
            worker_id="test-worker:1",
        )

    async def _one_pending_callback(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_recording: Recording,
    ) -> int:
        await _seed_upload(session_factory, seeded_recording.id)
        async with session_factory() as session, session.begin():
            await enqueue_recording_callbacks(
                session,
                tenant_id=_TENANT,
                recording_id=seeded_recording.id,
                generation=1,
                status="indexed",
            )
        async with session_factory() as session:
            row = (await session.execute(select(IntegrationCallback))).scalar_one()
            return int(row.id)

    @pytest.mark.asyncio
    @respx.mock
    async def test_delivery_signs_the_body_the_receiver_can_verify(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_recording: Recording,
    ) -> None:
        callback_id = await self._one_pending_callback(session_factory, seeded_recording)
        route = respx.post("https://receiver.example/hook").mock(return_value=httpx.Response(200))

        attempted = await self._worker(session_factory).run_once()
        assert attempted == 1
        assert route.called

        request = route.calls.last.request
        signature = request.headers["X-AudioGraphy-Signature"]
        timestamp, _, mac = signature.partition(",")
        seconds = timestamp.removeprefix("t=")
        # Verify exactly as the integration guide tells the receiver to.
        async with session_factory() as session:
            key_id = (await session.execute(select(IntegrationUpload.api_key_id))).scalar_one()
        secret = derive_webhook_secret(b"unit-test-signing-root", key_id)
        expected = hmac.new(
            secret.encode(), f"{seconds}.".encode() + request.content, hashlib.sha256
        ).hexdigest()
        assert mac == f"v1={expected}"

        payload = json.loads(request.content)
        assert payload["event_type"] == "recording.indexed"
        assert request.headers["X-AudioGraphy-Delivery"] == payload["event_id"]

        async with session_factory() as session:
            row = await session.get(IntegrationCallback, callback_id)
            assert row is not None
            assert row.status == "succeeded"
            assert row.completed_at is not None

    @pytest.mark.asyncio
    @respx.mock
    async def test_failures_back_off_and_end_in_dead_letter(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_recording: Recording,
    ) -> None:
        callback_id = await self._one_pending_callback(session_factory, seeded_recording)
        respx.post("https://receiver.example/hook").mock(
            return_value=httpx.Response(503, text="maintenance")
        )
        worker = self._worker(session_factory)

        for expected_attempts in range(1, 6):
            assert await worker.run_once() == 1
            async with session_factory() as session, session.begin():
                row = await session.get(IntegrationCallback, callback_id)
                assert row is not None
                assert row.attempts == expected_attempts
                assert "HTTP 503" in (row.last_error or "")
                if expected_attempts < 5:
                    assert row.status == "failed"
                    # Make the backoff due immediately so the next sweep retries.
                    row.available_at = datetime.now(UTC) - timedelta(seconds=1)
                else:
                    # The receiver being down forever must become VISIBLE,
                    # not an infinite retry loop nor a silent drop.
                    assert row.status == "dead_letter"

        assert await worker.run_once() == 0, "dead_letter rows are never re-claimed"


class TestTargetValidation:
    def test_public_targets_pass_and_private_ones_do_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import socket

        # Never trust the test host's resolver: some networks answer every
        # name (captive DNS), which turned this into an environment lottery.
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, *_a, **_k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        validate_callback_url("https://hooks.example.com/x", allow_private=False)
        monkeypatch.undo()
        for hostile in (
            "http://127.0.0.1/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.8/x",
            "ftp://example.com/x",
        ):
            with pytest.raises(ValueError):
                validate_callback_url(hostile, allow_private=False)
        # 私有化内网是合法场景——显式开关后放行。
        validate_callback_url("http://10.0.0.8/x", allow_private=True)


class TestPipelineEmitsOnTerminalStates:
    @pytest.mark.asyncio
    async def test_reaching_indexed_enqueues_the_callback_in_the_same_commit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle,
        vector_store,
        graph_store,
        file_index,
        seeded_recording: Recording,
    ) -> None:
        """The whole point of the outbox: the terminal status and the promise
        to report it commit together. Runs the real pipeline to `indexed` and
        finds the callback row it must have left behind."""

        from audio_graphy.services.indexing import IndexingService

        await _seed_upload(session_factory, seeded_recording.id)
        service = IndexingService(
            session_factory, mock_bundle, vector_store, graph_store, file_index
        )
        await service.run_pipeline(seeded_recording)

        async with session_factory() as session:
            recording = await session.get(Recording, seeded_recording.id)
            assert recording is not None
            assert recording.status == "indexed"
            callback = (await session.execute(select(IntegrationCallback))).scalar_one()
            assert callback.payload["event_type"] == "recording.indexed"
            assert callback.payload["external_ref"] == "crm-cb-1"
            assert callback.status == "pending"
            # The SSE feed rides the same transaction: one domain event, with
            # the agent filter's key present so the stream can scope agents.
            from audio_graphy.models.domain_event import DomainEvent

            domain_event = (await session.execute(select(DomainEvent))).scalar_one()
            assert domain_event.event_type == "recording.indexed"
            assert "agent_user_id" in domain_event.payload
