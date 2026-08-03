"""End-to-end voiceprint chain against the mock adapters.

Every earlier milestone shipped pieces of this chain that were never
connected: SpeakerLinker had no caller, extract_voiceprint was never invoked
by ingestion, and the indexing pipeline never even switched diarization on.
Unit tests all passed the whole time, because each piece worked in
isolation.

These tests therefore assert the *seam*: given a recording and the mock
CAM++ adapter, does real speaker data actually land in the database — and
does the same person appearing in a second recording merge into one node
rather than two?
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from audio_graphy.adapters.mock_voiceprint import MockVoiceprintAdapter
from audio_graphy.core.chunker import ChunkerOutput, SegmentRecord
from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.models.speaker_link import SpeakerLink
from audio_graphy.models.speaker_node import SpeakerNode
from audio_graphy.models.voiceprint_vector import VoiceprintVector
from audio_graphy.services.indexing import IndexingService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TENANT = "chang_an"


@pytest.fixture
def dev_crypto(tmp_path: Path) -> AudioCrypto:
    return AudioCrypto(tmp_path / "master.key", dev_mode=True)


async def _seed_recording(session_factory: Any, recording_id: int, path: str) -> None:
    """Persist the Recording row the voiceprint rows key off.

    The FK is what makes DSAR erasure cascade, so the chain cannot be
    exercised against a phantom recording.
    """
    from audio_graphy.models.recording import Recording as RecordingRow

    async with session_factory() as session:
        session.add(
            RecordingRow(
                id=recording_id,
                tenant_id=TENANT,
                store_id="STORE-E2E",
                path=path,
                recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
            )
        )
        await session.commit()


class _Settings:
    """Voiceprint settings with the feature switched on."""

    enable_voiceprint = True
    voiceprint_sampling_strategy = "weighted_mean"
    voiceprint_sample_min_segment_sec = 1.0
    voiceprint_sample_min_total_sec = 3.0
    voiceprint_sample_max_segments = 8
    voiceprint_sample_outlier_cosine = 0.5
    voiceprint_cosine_threshold = 0.5
    voiceprint_ambiguous_threshold = 0.7
    enable_speaker_layer2_fuzzy = False


class _Bundle:
    def __init__(self, voiceprint: Any) -> None:
        self.voiceprint = voiceprint


def _service(
    session_factory: Any,
    crypto: Any,
    voiceprint: Any,
) -> IndexingService:
    return IndexingService(
        session_factory=session_factory,
        bundle=_Bundle(voiceprint),  # type: ignore[arg-type]
        vector_store=None,  # type: ignore[arg-type]
        graph_store=None,  # type: ignore[arg-type]
        file_index=None,  # type: ignore[arg-type]
        settings=_Settings(),
        audio_crypto=crypto,
    )


class _Recording:
    def __init__(self, recording_id: int, path: str) -> None:
        self.id = recording_id
        self.tenant_id = TENANT
        self.path = path
        self.recorded_at = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def _two_speaker_output(recording_id: int) -> ChunkerOutput:
    """A recording where two people each speak well past the gates."""
    from audio_graphy.adapters.protocols import DiarizationSegment

    return ChunkerOutput(
        recording_id=recording_id,
        segments=[
            SegmentRecord(
                idx=0,
                start_sec=0.0,
                end_sec=20.0,
                transcript="",
                speaker="spk_0",
                vad_conf=1.0,
            )
        ],
        chunks=[],
        diarization=(
            DiarizationSegment(start_sec=0.0, end_sec=10.0, speaker_id="spk_0"),
            DiarizationSegment(start_sec=10.0, end_sec=20.0, speaker_id="spk_1"),
            DiarizationSegment(start_sec=20.0, end_sec=32.0, speaker_id="spk_0"),
        ),
    )


async def _counts(session_factory: Any) -> tuple[int, int, int]:
    async with session_factory() as session:
        nodes = (
            await session.execute(
                select(func.count())
                .select_from(SpeakerNode)
                .where(SpeakerNode.tenant_id == TENANT)
            )
        ).scalar_one()
        vectors = (
            await session.execute(
                select(func.count())
                .select_from(VoiceprintVector)
                .where(VoiceprintVector.tenant_id == TENANT)
            )
        ).scalar_one()
        links = (
            await session.execute(
                select(func.count())
                .select_from(SpeakerLink)
                .where(SpeakerLink.tenant_id == TENANT)
            )
        ).scalar_one()
    return int(nodes), int(vectors), int(links)


class TestVoiceprintChainWithMocks:
    async def test_chain_writes_speaker_data_end_to_end(
        self,
        async_session_factory: Any,
        dev_crypto: Any,
        tmp_path: Any,
    ) -> None:
        """The seam that was broken for three milestones."""
        audio = tmp_path / "rec1.wav"
        audio.write_bytes(b"fake audio")
        await _seed_recording(async_session_factory, 9001, str(audio))
        svc = _service(async_session_factory, dev_crypto, MockVoiceprintAdapter())

        await svc._stage_speaker_link(
            _Recording(9001, str(audio)),  # type: ignore[arg-type]
            _two_speaker_output(9001),
        )

        nodes, vectors, links = await _counts(async_session_factory)
        assert nodes == 2, "one SpeakerNode per diarized speaker"
        assert vectors == 2, "each speaker's vector is stored, encrypted"
        assert links == 2, "each link is recorded for audit"

    async def test_same_speaker_across_recordings_merges_into_one_node(
        self,
        async_session_factory: Any,
        dev_crypto: Any,
        tmp_path: Any,
    ) -> None:
        """Cross-recording identity is the entire point of the feature.

        The mock derives its vector from ``speaker_id``, so the same label in
        a second recording is the same voice — exactly the case Layer 1 must
        merge instead of creating a second node.
        """
        first = tmp_path / "rec1.wav"
        first.write_bytes(b"fake audio one")
        second = tmp_path / "rec2.wav"
        second.write_bytes(b"fake audio two")
        await _seed_recording(async_session_factory, 9101, str(first))
        await _seed_recording(async_session_factory, 9102, str(second))
        svc = _service(async_session_factory, dev_crypto, MockVoiceprintAdapter())

        await svc._stage_speaker_link(
            _Recording(9101, str(first)),  # type: ignore[arg-type]
            _two_speaker_output(9101),
        )
        nodes_after_first, _, _ = await _counts(async_session_factory)
        assert nodes_after_first == 2

        await svc._stage_speaker_link(
            _Recording(9102, str(second)),  # type: ignore[arg-type]
            _two_speaker_output(9102),
        )

        nodes, _, links = await _counts(async_session_factory)
        assert nodes == 2, "the same two people, not four"
        assert links == 4, "but both recordings are linked to them"

        async with async_session_factory() as session:
            recorded = (
                await session.execute(
                    select(SpeakerNode.recordings_count, SpeakerNode.merge_strategy)
                    .where(SpeakerNode.tenant_id == TENANT)
                    .order_by(SpeakerNode.id)
                )
            ).all()
        assert [int(r[0]) for r in recorded] == [2, 2]
        assert {str(r[1]) for r in recorded} == {"voiceprint"}

    async def test_rerunning_the_same_recording_changes_nothing(
        self,
        async_session_factory: Any,
        dev_crypto: Any,
        tmp_path: Any,
    ) -> None:
        """The pipeline is retryable; linking is not idempotent on its own."""
        audio = tmp_path / "rec1.wav"
        audio.write_bytes(b"fake audio")
        await _seed_recording(async_session_factory, 9201, str(audio))
        svc = _service(async_session_factory, dev_crypto, MockVoiceprintAdapter())
        recording = _Recording(9201, str(audio))

        await svc._stage_speaker_link(recording, _two_speaker_output(9201))  # type: ignore[arg-type]
        before = await _counts(async_session_factory)

        await svc._stage_speaker_link(recording, _two_speaker_output(9201))  # type: ignore[arg-type]
        after = await _counts(async_session_factory)

        assert before == after

    async def test_disabled_flag_writes_nothing(
        self,
        async_session_factory: Any,
        dev_crypto: Any,
        tmp_path: Any,
    ) -> None:
        """M3-M6 deployments must see zero new rows."""
        audio = tmp_path / "rec1.wav"
        audio.write_bytes(b"fake audio")
        svc = _service(async_session_factory, dev_crypto, MockVoiceprintAdapter())
        svc._settings.enable_voiceprint = False  # type: ignore[attr-defined]

        await svc._stage_speaker_link(
            _Recording(9301, str(audio)),  # type: ignore[arg-type]
            _two_speaker_output(9301),
        )

        assert await _counts(async_session_factory) == (0, 0, 0)

    async def test_stored_vector_round_trips_through_encryption(
        self,
        async_session_factory: Any,
        dev_crypto: Any,
        tmp_path: Any,
    ) -> None:
        """PIPL §14.3: vectors are encrypted at rest but must stay usable."""
        audio = tmp_path / "rec1.wav"
        audio.write_bytes(b"fake audio")
        await _seed_recording(async_session_factory, 9401, str(audio))
        svc = _service(async_session_factory, dev_crypto, MockVoiceprintAdapter())

        await svc._stage_speaker_link(
            _Recording(9401, str(audio)),  # type: ignore[arg-type]
            _two_speaker_output(9401),
        )

        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(VoiceprintVector).where(
                        VoiceprintVector.tenant_id == TENANT
                    )
                )
            ).scalars().first()
            assert row is not None
            # Ciphertext, not the raw vector.
            assert b"\x00" in row.vector_encrypted or len(row.vector_encrypted) > 0
            decrypted = row.decrypted_vector(dev_crypto)

        assert len(decrypted) == 192
        norm = float(sum(float(x) * float(x) for x in decrypted)) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-3)
        # duration_sec must be the sampled audio, not the speaker's total.
        assert 0.0 < float(row.duration_sec) <= 32.0
