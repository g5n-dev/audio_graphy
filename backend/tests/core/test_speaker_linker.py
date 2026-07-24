"""Unit tests for SpeakerLinker — 3-layer voiceprint matching (M7 WS-2).

Covers:
- Pure helpers: ``_cosine``, ``hash_voiceprint``, ``derive_role_hint``.
- SpeakerLinker.run() against an in-memory async DB:
  - Layer 1 unambiguous merge (cos ≥ 0.7).
  - Layer 1 ambiguous merge (0.5 ≤ cos < 0.7).
  - Layer 1 no-match → new SpeakerNode creation.
  - Empty candidate list short-circuit.
  - Threshold invariant (vp_threshold ≤ ambiguity_threshold).
  - Multi-candidate batch within one run.
- ``link_speakers`` M7 stub returns [].
"""

from __future__ import annotations

import hashlib
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.core.speaker_linker import (
    SpeakerLinker,
    SpeakerLinkReport,
    _cosine,
    _NewSpeakerCandidate,
    derive_role_hint,
    hash_voiceprint,
)
from audio_graphy.models.recording import Recording
from audio_graphy.models.speaker_link import SpeakerLink
from audio_graphy.models.speaker_node import SpeakerNode
from audio_graphy.models.voiceprint_vector import VoiceprintVector

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def dev_crypto(tmp_path: Path) -> AudioCrypto:
    """AudioCrypto in dev mode (auto-generates a master key)."""
    return AudioCrypto(tmp_path / "master.key", dev_mode=True)


def _make_vector(seed: float, dim: int = 192) -> tuple[float, ...]:
    """Build a deterministic L2-normalized vector for testing.

    Uses a hash-seeded LCG over the component index so that different seeds
    produce statistically independent vectors. Two seeds that differ by
    a small Δ will produce nearly-identical vectors (cos ≈ 1).
    """
    import hashlib
    import math
    import struct

    # Seed: hash(seed) → bytes → expand into dim floats.
    h = hashlib.sha256(struct.pack("<d", float(seed))).digest()
    # Repeat hash to get enough bytes for dim * 4.
    buf = bytearray()
    counter = 0
    while len(buf) < dim * 4:
        buf.extend(hashlib.sha256(h + counter.to_bytes(4, "little")).digest())
        counter += 1
    raw = []
    for i in range(dim):
        # Map 4 bytes to [-1, 1] uniformly.
        u = int.from_bytes(buf[i * 4 : i * 4 + 4], "little") / (2**32 - 1)
        raw.append(u * 2.0 - 1.0)
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return tuple(x / norm for x in raw)


def _vector_with_noise(
    base: tuple[float, ...],
    *,
    noise: float = 0.001,
    seed: int = 0,
) -> tuple[float, ...]:
    """Return a perturbed copy of ``base`` for testing near-duplicate merges."""
    import hashlib
    import math

    h = hashlib.sha256(b"noise" + seed.to_bytes(4, "little")).digest()
    buf = h * ((len(base) // 8) + 1)
    out = []
    for i, x in enumerate(base):
        u = int.from_bytes(buf[i * 2 : i * 2 + 2], "little") / 65535.0
        out.append(x + (u - 0.5) * 2.0 * noise)
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return tuple(x / norm for x in out)


def _make_candidate(
    seed: float,
    *,
    speaker_id: str = "spk_0",
    recording_id: int = 1,
    speech_sec: float = 5.0,
    first_seen: datetime | None = None,
    role_hint: str = "agent",
) -> _NewSpeakerCandidate:
    """Build a candidate using a deterministic seed vector."""
    vec = _make_vector(seed)
    return _NewSpeakerCandidate(
        speaker_id=speaker_id,
        voiceprint=vec,
        voiceprint_id=hash_voiceprint(vec),
        recording_id=recording_id,
        speech_sec=speech_sec,
        first_seen=first_seen,
        role_hint=role_hint,
    )


async def _insert_recording(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    recording_id: int = 1,
    tenant_id: str = "default",
) -> int:
    """Insert a minimal Recording row so FK constraints pass.

    Returns the autoincrement ``id`` of the new row.
    """
    async with session_factory() as session:
        rec = Recording(
            tenant_id=tenant_id,
            store_id=f"STORE-{recording_id:04d}",
            path=f"/data/audio/rec-{recording_id:04d}.wav.enc",
            audio_encryption_meta={"algo": "AES-256-GCM"},
            recorded_at=datetime.now(UTC),
        )
        session.add(rec)
        await session.commit()
        return int(rec.id)


# ============================================================
# Pure helpers
# ============================================================


class TestCosine:
    def test_identical_vectors_cos_one(self) -> None:
        v = _make_vector(0.5)
        assert _cosine(v, v) == pytest.approx(1.0, abs=1e-9)

    def test_orthogonal_vectors_cos_zero(self) -> None:
        """Two L2-norm vectors with disjoint support → cos ≈ 0."""
        a = (1.0, 0.0, 0.0, 0.0)
        b = (0.0, 1.0, 0.0, 0.0)
        assert _cosine(a, b) == pytest.approx(0.0, abs=1e-12)

    def test_different_length_returns_neg_one(self) -> None:
        assert _cosine((1.0, 2.0), (1.0,)) == -1.0

    def test_empty_vectors_returns_neg_one(self) -> None:
        assert _cosine((), ()) == -1.0

    def test_zero_norm_returns_neg_one(self) -> None:
        assert _cosine((0.0, 0.0), (1.0, 1.0)) == -1.0


class TestHashVoiceprint:
    def test_hash_matches_sha256_of_float32_pack(self) -> None:
        v = _make_vector(1.0)
        expected = hashlib.sha256(struct.pack(f"<{len(v)}f", *v)).hexdigest()
        assert hash_voiceprint(v) == expected

    def test_hash_is_64_chars_hex(self) -> None:
        h = hash_voiceprint(_make_vector(2.0))
        assert len(h) == 64
        int(h, 16)  # must parse as hex

    def test_hash_changes_when_vector_changes(self) -> None:
        assert hash_voiceprint(_make_vector(1.0)) != hash_voiceprint(_make_vector(2.0))


class TestDeriveRoleHint:
    def test_empty_returns_empty(self) -> None:
        assert derive_role_hint([]) == {}

    def test_single_speaker_unknown(self) -> None:
        result = derive_role_hint([("spk_0", 30.0)])
        assert result == {"spk_0": "unknown"}

    def test_two_speakers_clear_agent(self) -> None:
        """70% / 30% split → longer is agent, shorter is customer."""
        result = derive_role_hint([("spk_0", 70.0), ("spk_1", 30.0)])
        assert result == {"spk_0": "agent", "spk_1": "customer"}

    def test_two_speakers_below_60_pct_unknown(self) -> None:
        """55% / 45% — neither dominates, both unknown."""
        result = derive_role_hint([("spk_0", 55.0), ("spk_1", 45.0)])
        assert result == {"spk_0": "unknown", "spk_1": "unknown"}

    def test_three_speakers_all_unknown(self) -> None:
        result = derive_role_hint([("a", 30.0), ("b", 20.0), ("c", 10.0)])
        assert result == {"a": "unknown", "b": "unknown", "c": "unknown"}

    def test_aggregates_durations_across_segments(self) -> None:
        """Two segments from spk_0 should be summed before role decision."""
        result = derive_role_hint(
            [("spk_0", 30.0), ("spk_1", 20.0), ("spk_0", 40.0), ("spk_1", 10.0)]
        )
        # spk_0 = 70, spk_1 = 30; 70/(70+30) = 0.7 ≥ 0.6
        assert result == {"spk_0": "agent", "spk_1": "customer"}

    def test_zero_total_returns_unknown(self) -> None:
        result = derive_role_hint([("spk_0", 0.0), ("spk_1", 0.0)])
        assert result == {"spk_0": "unknown", "spk_1": "unknown"}


# ============================================================
# SpeakerLinker construction
# ============================================================


@pytest.mark.integration
class TestSpeakerLinkerConstruction:
    def test_threshold_invariant_violated(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        dev_crypto: AudioCrypto,
    ) -> None:
        with pytest.raises(ValueError, match="voiceprint_threshold"):
            SpeakerLinker(
                async_session_factory,
                dev_crypto,
                voiceprint_threshold=0.8,
                ambiguity_threshold=0.5,
            )

    def test_default_thresholds(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        dev_crypto: AudioCrypto,
    ) -> None:
        linker = SpeakerLinker(async_session_factory, dev_crypto)
        assert linker._vp_threshold == 0.5
        assert linker._ambiguity_threshold == 0.7

    def test_custom_tenant(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        dev_crypto: AudioCrypto,
    ) -> None:
        linker = SpeakerLinker(async_session_factory, dev_crypto, tenant_id="tenant-xyz")
        assert linker._tenant_id == "tenant-xyz"


# ============================================================
# SpeakerLinker.run() integration
# ============================================================


@pytest_asyncio.fixture
async def linker(
    async_session_factory: async_sessionmaker[AsyncSession],
    dev_crypto: AudioCrypto,
) -> SpeakerLinker:
    """Standard SpeakerLinker with default thresholds."""
    return SpeakerLinker(async_session_factory, dev_crypto)


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpeakerLinkerRunEmpty:
    async def test_empty_candidates_returns_zero_report(
        self,
        linker: SpeakerLinker,
    ) -> None:
        """Empty candidate list short-circuits with zero counts."""
        report = await linker.run(recording_id=1, candidates=[])
        assert isinstance(report, SpeakerLinkReport)
        assert report.recording_id == 1
        assert report.new_speakers == 0
        assert report.merged_speakers == 0
        assert report.ambiguous_merges == 0
        assert report.fuzzy_merges == 0


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpeakerLinkerNewSpeaker:
    async def test_creates_new_speaker_node(
        self,
        linker: SpeakerLinker,
        async_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """First-time candidate → brand-new SpeakerNode + VoiceprintVector + SpeakerLink."""
        rec_id = await _insert_recording(async_session_factory, recording_id=1)
        cand = _make_candidate(seed=1.0, recording_id=rec_id, speech_sec=12.5)

        report = await linker.run(recording_id=rec_id, candidates=[cand])

        assert report.new_speakers == 1
        assert report.merged_speakers == 0
        assert report.ambiguous_merges == 0

        async with async_session_factory() as session:
            nodes = (await session.execute(select(SpeakerNode))).scalars().all()
            assert len(nodes) == 1
            node = nodes[0]
            assert node.voiceprint_id == cand.voiceprint_id
            assert node.speaker_role == "agent"
            assert node.recordings_list == [rec_id]
            assert node.recordings_count == 1
            assert node.total_speech_sec == pytest.approx(12.5)
            assert node.merge_strategy == "single_recording"
            assert node.ambiguity_tag is None

            vps = (await session.execute(select(VoiceprintVector))).scalars().all()
            assert len(vps) == 1
            assert vps[0].voiceprint_id == cand.voiceprint_id
            assert vps[0].duration_sec == pytest.approx(12.5)

            links = (await session.execute(select(SpeakerLink))).scalars().all()
            assert len(links) == 1
            assert links[0].strategy == "single_recording"

    async def test_new_speaker_display_name_uses_vp_prefix(
        self,
        linker: SpeakerLinker,
        async_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Display name follows ``speaker:vp_xxxxxxxx`` convention (L1)."""
        rec_id = await _insert_recording(async_session_factory, recording_id=1)
        cand = _make_candidate(seed=2.0, recording_id=rec_id)

        await linker.run(recording_id=rec_id, candidates=[cand])

        async with async_session_factory() as session:
            node = (await session.execute(select(SpeakerNode))).scalar_one()
            assert node.display_name == f"speaker:vp_{cand.voiceprint_id[:8]}"


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpeakerLinkerMergeUnambiguous:
    async def test_high_cosine_merges_without_ambiguity_tag(
        self,
        linker: SpeakerLinker,
        async_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Two candidates with near-identical vectors → merge with cos ≥ 0.7 (no tag)."""
        rec1 = await _insert_recording(async_session_factory, recording_id=1)
        rec2 = await _insert_recording(async_session_factory, recording_id=2)

        # Same speaker across two recordings — voiceprint extracted with
        # minor noise → cos should be ≥ 0.95.
        base_vec = _make_vector(seed=1.0)
        cand1 = _NewSpeakerCandidate(
            speaker_id="spk_0",
            voiceprint=base_vec,
            voiceprint_id=hash_voiceprint(base_vec),
            recording_id=rec1,
            speech_sec=10.0,
            first_seen=None,
            role_hint="agent",
        )
        near_dup = _vector_with_noise(base_vec, noise=0.05, seed=42)
        cand2 = _NewSpeakerCandidate(
            speaker_id="spk_0",
            voiceprint=near_dup,
            voiceprint_id=hash_voiceprint(near_dup),
            recording_id=rec2,
            speech_sec=10.0,
            first_seen=None,
            role_hint="agent",
        )

        await linker.run(recording_id=rec1, candidates=[cand1])
        report = await linker.run(recording_id=rec2, candidates=[cand2])

        assert report.new_speakers == 0
        assert report.merged_speakers == 1
        assert report.ambiguous_merges == 0

        async with async_session_factory() as session:
            nodes = (await session.execute(select(SpeakerNode))).scalars().all()
            assert len(nodes) == 1
            node = nodes[0]
            assert node.recordings_count == 2
            assert sorted(node.recordings_list) == sorted([rec1, rec2])
            assert node.merge_strategy == "voiceprint"
            assert node.ambiguity_tag is None
            assert node.merge_confidence >= 0.7

            vps = (await session.execute(select(VoiceprintVector))).scalars().all()
            assert len(vps) == 2  # one voiceprint per recording

            links = (await session.execute(select(SpeakerLink))).scalars().all()
            assert len(links) == 2
            merge_links = [lk for lk in links if lk.strategy == "voiceprint"]
            assert len(merge_links) == 1
            assert merge_links[0].cosine_similarity is not None
            assert merge_links[0].cosine_similarity >= 0.7


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpeakerLinkerMergeAmbiguous:
    async def test_medium_cosine_tags_ambiguous(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        dev_crypto: AudioCrypto,
    ) -> None:
        """cos ∈ [vp_threshold, ambiguity_threshold) → tag 'AMBIGUOUS' (Q2)."""
        rec1 = await _insert_recording(async_session_factory, recording_id=1)
        rec2 = await _insert_recording(async_session_factory, recording_id=2)

        # Build two near-duplicate vectors with controlled noise so the
        # cosine lands in [0.5, 0.7) — the AMBIGUOUS band at default
        # thresholds. noise=0.3 produces cos ≈ 0.54.
        base_vec = _make_vector(seed=1.0)
        noisy = _vector_with_noise(base_vec, noise=0.3, seed=99)
        linker = SpeakerLinker(
            async_session_factory,
            dev_crypto,
            voiceprint_threshold=0.5,
            ambiguity_threshold=0.7,
        )
        cand1 = _NewSpeakerCandidate(
            speaker_id="spk_0",
            voiceprint=base_vec,
            voiceprint_id=hash_voiceprint(base_vec),
            recording_id=rec1,
            speech_sec=10.0,
            first_seen=None,
            role_hint="agent",
        )
        cand2 = _NewSpeakerCandidate(
            speaker_id="spk_0",
            voiceprint=noisy,
            voiceprint_id=hash_voiceprint(noisy),
            recording_id=rec2,
            speech_sec=10.0,
            first_seen=None,
            role_hint="agent",
        )

        await linker.run(recording_id=rec1, candidates=[cand1])
        report = await linker.run(recording_id=rec2, candidates=[cand2])

        assert report.merged_speakers == 1
        assert report.ambiguous_merges == 1

        async with async_session_factory() as session:
            node = (await session.execute(select(SpeakerNode))).scalar_one()
            assert node.ambiguity_tag == "AMBIGUOUS"
            assert node.merge_strategy == "voiceprint"


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpeakerLinkerNoMatch:
    async def test_below_threshold_creates_new_speaker(
        self,
        linker: SpeakerLinker,
        async_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """cos < 0.5 → no merge; both speakers remain separate."""
        rec1 = await _insert_recording(async_session_factory, recording_id=1)
        rec2 = await _insert_recording(async_session_factory, recording_id=2)

        # Two independent hash-seeded vectors → cos near 0.
        cand1 = _make_candidate(seed=1.0, recording_id=rec1, speaker_id="spk_0")
        cand2 = _make_candidate(seed=999999.0, recording_id=rec2, speaker_id="spk_0")

        await linker.run(recording_id=rec1, candidates=[cand1])
        report = await linker.run(recording_id=rec2, candidates=[cand2])

        assert report.new_speakers == 1
        assert report.merged_speakers == 0

        async with async_session_factory() as session:
            nodes = (await session.execute(select(SpeakerNode))).scalars().all()
            assert len(nodes) == 2


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpeakerLinkerMultiCandidate:
    async def test_three_candidates_one_match_two_new(
        self,
        linker: SpeakerLinker,
        async_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """3 candidates: 1 matches existing, 2 are new."""
        rec1 = await _insert_recording(async_session_factory, recording_id=1)
        rec2 = await _insert_recording(async_session_factory, recording_id=2)

        # Recording 1 has one speaker.
        base_vec = _make_vector(seed=7.0)
        cand_r1 = _NewSpeakerCandidate(
            speaker_id="spk_0",
            voiceprint=base_vec,
            voiceprint_id=hash_voiceprint(base_vec),
            recording_id=rec1,
            speech_sec=10.0,
            first_seen=None,
            role_hint="agent",
        )
        await linker.run(recording_id=rec1, candidates=[cand_r1])

        # Recording 2 has three speakers; one near-duplicate matches cand_r1.
        near_dup = _vector_with_noise(base_vec, noise=0.05, seed=11)
        cand_match = _NewSpeakerCandidate(
            speaker_id="spk_0",
            voiceprint=near_dup,
            voiceprint_id=hash_voiceprint(near_dup),
            recording_id=rec2,
            speech_sec=10.0,
            first_seen=None,
            role_hint="agent",
        )
        # Two completely different speakers.
        cand_new_a = _make_candidate(seed=12345.0, recording_id=rec2, speaker_id="spk_1")
        cand_new_b = _make_candidate(seed=67890.0, recording_id=rec2, speaker_id="spk_2")

        report = await linker.run(
            recording_id=rec2,
            candidates=[cand_match, cand_new_a, cand_new_b],
        )

        assert report.new_speakers == 2
        assert report.merged_speakers == 1

        async with async_session_factory() as session:
            nodes = (await session.execute(select(SpeakerNode))).scalars().all()
            assert len(nodes) == 3
            # The matched node should now have recordings_count == 2.
            multi_recordings = [n for n in nodes if n.recordings_count == 2]
            assert len(multi_recordings) == 1
            assert sorted(multi_recordings[0].recordings_list) == sorted([rec1, rec2])


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpeakerLinkerLinkSpeakersStub:
    async def test_link_speakers_returns_empty(
        self,
        linker: SpeakerLinker,
    ) -> None:
        """Batch-mode stub returns [] in M7."""
        result = await linker.link_speakers("default")
        assert result == []


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpeakerLinkerAudit:
    async def test_audit_writer_invoked(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        dev_crypto: AudioCrypto,
    ) -> None:
        """AuditWriter.record() is called for each merge / create decision."""
        recorded: list[dict] = []

        class _StubAudit:
            async def record(
                self,
                *,
                tenant_id: str,
                user_id: object,
                action: str,
                target: str,
                before: dict,
                after: dict,
            ) -> None:
                recorded.append(
                    {
                        "tenant_id": tenant_id,
                        "action": action,
                        "target": target,
                        "before": before,
                        "after": after,
                    }
                )

        linker = SpeakerLinker(
            async_session_factory,
            dev_crypto,
            audit=_StubAudit(),  # type: ignore[arg-type]
        )
        rec_id = await _insert_recording(async_session_factory, recording_id=1)
        cand = _make_candidate(seed=1.0, recording_id=rec_id)

        report = await linker.run(recording_id=rec_id, candidates=[cand])

        assert report.audit_written == 1
        assert len(recorded) == 1
        assert recorded[0]["action"] == "speaker.create"
        assert recorded[0]["tenant_id"] == "default"

    async def test_no_audit_writer_skips_audit(
        self,
        linker: SpeakerLinker,
        async_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No AuditWriter → audit_written stays 0 and run still succeeds."""
        rec_id = await _insert_recording(async_session_factory, recording_id=1)
        cand = _make_candidate(seed=1.0, recording_id=rec_id)

        report = await linker.run(recording_id=rec_id, candidates=[cand])
        assert report.audit_written == 0
        assert report.new_speakers == 1
