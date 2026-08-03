"""SpeakerLinker — cross-recording speaker merging via 3-layer strategy.

M7 architecture §8.

Layer 1 — Voiceprint cosine (primary):
    ``cos(new.voiceprint, existing.voiceprint) ≥ voiceprint_cosine_threshold``
    (default 0.5, L9 locked).
    - ``cos ≥ voiceprint_ambiguous_threshold`` (default 0.7): merge with
      ``ambiguity_tag=None``.
    - ``0.5 ≤ cos < 0.7``: merge with ``ambiguity_tag="AMBIGUOUS"``.

Layer 2 — EntityMerger fuzzy (auxiliary, M7 stub):
    fuzz.WRatio on speaker display_name. M7 returns ``None`` — full impl
    deferred to M8 (needs admin UI for confirmation flow). Hook kept
    so the call site is stable.

Layer 3 — Admin manual confirm (M7 stub):
    Logs the pending decision; M8+ exposes the API.

Flow per ``run(recording_id)``::

    1. Receive candidates from ``core.voiceprint_sampler.VoiceprintSampler``
       (one per speaker that cleared the sampling quality gates).
    2. For each candidate:
        a. Iterate all existing SpeakerNodes in the same tenant.
        b. Apply Layer 1: pick the highest-cosine match above threshold.
        c. If no Layer-1 match: create new SpeakerNode (strategy=single_recording).
    3. Insert VoiceprintVector rows (encrypted via AudioCrypto).
    4. Insert SpeakerLink rows (audit trail).
    5. Return SpeakerLinkReport.

How a candidate's vector is sampled — and why the representative template
is a speaker's longest sample rather than their newest — is decided in
``docs/adr/0001-voiceprint-sampling.md``.

Deviation note (round 1):
    Architecture §8 mentions "EntityMerger fuzzy layer" reuse. In practice
    EntityMerger is a per-tenant, in-memory, ``merge()``-driven cache — it
    is shaped for entity-name normalisation during *extraction*, not for
    cross-recording speaker lookup. Pulling it in here would require a
    permanent session and could not be safely used from a nightly cron.
    M7 therefore keeps the Layer-2 hook signature but stubs the body.
    M8 will introduce a dedicated ``SpeakerFuzzyMatcher`` that reuses
    rapidfuzz without the alias-write side effects.
"""

from __future__ import annotations

import hashlib
import logging
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.speaker_link import SpeakerLink
from audio_graphy.models.speaker_node import SpeakerNode
from audio_graphy.models.voiceprint_vector import VoiceprintVector

if TYPE_CHECKING:
    from audio_graphy.core.audit import AuditWriter
    from audio_graphy.core.crypto import AudioCrypto

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpeakerLinkReport:
    """Output of one ``SpeakerLinker.run(recording_id)`` call.

    Attributes:
        recording_id: Source recording ID.
        new_speakers: Number of brand-new SpeakerNodes created.
        merged_speakers: Number of speakers merged into existing SpeakerNodes.
        ambiguous_merges: Subset of merges tagged ``AMBIGUOUS``.
        fuzzy_merges: Subset of merges via Layer 2 (rapidfuzz). Always 0 in M7.
        audit_written: Number of audit_log rows inserted (best-effort count).
        m9_pending_reconfirm: M9 — count of SpeakerMergePending rows enqueued
            by Layer 2 (AMBIGUOUS verdicts awaiting voiceprint reconfirm).
    """

    recording_id: int
    new_speakers: int
    merged_speakers: int
    ambiguous_merges: int = 0
    fuzzy_merges: int = 0
    audit_written: int = 0
    m9_pending_reconfirm: int = 0


@dataclass(frozen=True, slots=True)
class _NewSpeakerCandidate:
    """One per-recording speaker extracted from diarization, awaiting linking.

    Attributes:
        speaker_id: Diarization-local label (``spk_0`` / ``spk_1``).
        voiceprint: 192-d L2-normalized vector (float tuple).
        voiceprint_id: sha256(voiceprint) hex.
        recording_id: Source recording.
        speech_sec: Total speech seconds (for ``total_speech_sec`` accumulation) —
            how much this person talked, including segments too short to sample.
        sampled_sec: Seconds of audio that actually produced ``voiceprint``.
            This, not ``speech_sec``, is the vector's quality signal: a caller
            with 200s of short interjections but only 3s of usable speech must
            not outrank a 120s monologue when picking a representative template.
        first_seen: Recording ``recorded_at`` (for ``SpeakerNode.first_seen``).
        role_hint: ``agent`` / ``customer`` / ``unknown`` from §17.9 heuristic.
        display_name: M9 R1 T12 — display name for Layer 2 fuzzy match.
            When empty, Layer 2 falls back to ``role_hint`` (M7 behaviour).
    """

    speaker_id: str
    voiceprint: tuple[float, ...]
    voiceprint_id: str
    recording_id: int
    speech_sec: float
    first_seen: datetime | None
    role_hint: str
    display_name: str = ""
    sampled_sec: float = 0.0


class SpeakerLinker:
    """Cross-recording speaker linking via 3-layer strategy.

    Args:
        session_factory: Async session maker for DB access.
        crypto: AudioCrypto instance for voiceprint encryption.
        audit: Optional AuditWriter for ``speaker.merge`` / ``speaker.create``
            events. ``None`` skips audit writes (used in tests).
        voiceprint_threshold: Layer-1 cosine threshold (default 0.5, L9).
        ambiguity_threshold: Cosine above which merges are unambiguous
            (default 0.7, Q2 locked).
        tenant_id: Tenant scope for the linker instance (one per tenant).
        enable_layer2_fuzzy: M9 R1 T12 — when True (default), invoke the
            ``SpeakerFuzzyMatcher`` (L8) on Layer-1 misses. When False,
            behave identically to M7 (zero-regression escape hatch).
        fuzzy_matcher: Optional pre-constructed SpeakerFuzzyMatcher
            (mainly for tests). When None and ``enable_layer2_fuzzy`` is
            True, a default matcher is constructed lazily on first use.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: AudioCrypto,
        audit: AuditWriter | None = None,
        *,
        voiceprint_threshold: float = 0.5,
        ambiguity_threshold: float = 0.7,
        tenant_id: str = "default",
        enable_layer2_fuzzy: bool = True,
        fuzzy_matcher: Any = None,
    ) -> None:
        if voiceprint_threshold > ambiguity_threshold:
            raise ValueError(
                f"voiceprint_threshold ({voiceprint_threshold}) must be ≤ "
                f"ambiguity_threshold ({ambiguity_threshold})"
            )
        self._session_factory = session_factory
        self._crypto = crypto
        self._audit = audit
        self._vp_threshold = voiceprint_threshold
        self._ambiguity_threshold = ambiguity_threshold
        self._tenant_id = tenant_id
        # M9 R1 T12 — Layer 2 fuzzy matcher wiring (L8 ruling).
        self._enable_layer2_fuzzy: bool = enable_layer2_fuzzy
        self._fuzzy_matcher: Any = fuzzy_matcher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def run(
        self,
        recording_id: int,
        candidates: Sequence[_NewSpeakerCandidate],
    ) -> SpeakerLinkReport:
        """Link all new speakers for one recording.

        Args:
            recording_id: The recording that just finished ingestion.
            candidates: One ``_NewSpeakerCandidate`` per diarization speaker
                in the recording. Caller is responsible for assembling these
                from the diarization timeline + voiceprint extraction results.

        Returns:
            SpeakerLinkReport with merge / new counts.
        """
        if not candidates:
            return SpeakerLinkReport(
                recording_id=recording_id,
                new_speakers=0,
                merged_speakers=0,
            )

        # Load all existing SpeakerNodes for this tenant (small N in practice).
        existing = await self._load_existing_speakers()

        new_count = 0
        merge_count = 0
        ambiguous = 0
        audit_written = 0
        fuzzy_count = 0
        pending_reconfirm = 0

        for cand in candidates:
            # Layer 1 — voiceprint cosine match.
            match = self._best_voiceprint_match(cand, existing)
            if match is not None:
                node, cosine, ambiguity_tag = match
                await self._merge_into_existing(cand, node, cosine, ambiguity_tag, recording_id)
                merge_count += 1
                if ambiguity_tag == "AMBIGUOUS":
                    ambiguous += 1
                audit_written += await self._write_audit(
                    action="speaker.merge",
                    target=f"speaker:{node.id}",
                    before={
                        "source_speaker_id": cand.speaker_id,
                        "voiceprint_id": cand.voiceprint_id,
                        "recording_id": recording_id,
                    },
                    after={
                        "canonical_speaker_id": node.id,
                        "cosine": cosine,
                        "ambiguity_tag": ambiguity_tag,
                    },
                )
                continue

            # Layer 2 — fuzzy name match (L8 wiring from M9 R1 T12).
            if self._enable_layer2_fuzzy:
                fuzzy_result = await self._try_layer2_fuzzy(cand, existing, recording_id)
                if fuzzy_result is not None:
                    node, ambiguity_tag, fuzzy_score, vp_score = fuzzy_result
                    if ambiguity_tag == "PENDING_REVIEW":
                        # A fuzzy observation is only a proposal.  Canonical
                        # node/vector/link state remains untouched until the
                        # review endpoint applies it transactionally.
                        pending_reconfirm += 1
                        audit_written += await self._write_audit(
                            action="speaker.merge.observed",
                            target=f"speaker:{node.id}",
                            before={
                                "source_speaker_id": cand.speaker_id,
                                "voiceprint_id": cand.voiceprint_id,
                                "recording_id": recording_id,
                                "layer": 2,
                            },
                            after={
                                "proposed_canonical_speaker_id": node.id,
                                "fuzzy_score": fuzzy_score,
                                "voiceprint_score": vp_score,
                                "state": "PENDING_REVIEW",
                            },
                        )
                        continue
                    await self._merge_into_existing(
                        cand,
                        node,
                        vp_score if vp_score is not None else 0.0,
                        ambiguity_tag,
                        recording_id,
                    )
                    merge_count += 1
                    fuzzy_count += 1
                    if ambiguity_tag == "AMBIGUOUS":
                        ambiguous += 1
                        pending_reconfirm += 1
                    audit_written += await self._write_audit(
                        action="speaker.merge",
                        target=f"speaker:{node.id}",
                        before={
                            "source_speaker_id": cand.speaker_id,
                            "voiceprint_id": cand.voiceprint_id,
                            "recording_id": recording_id,
                            "layer": 2,
                        },
                        after={
                            "canonical_speaker_id": node.id,
                            "fuzzy_score": fuzzy_score,
                            "voiceprint_score": vp_score,
                            "ambiguity_tag": ambiguity_tag,
                        },
                    )
                    continue

            # Layer 3 / fallback — create new SpeakerNode.
            node = await self._create_new_speaker(cand, recording_id)
            existing.append(node)
            new_count += 1
            audit_written += await self._write_audit(
                action="speaker.create",
                target=f"speaker:{node.id}",
                before={
                    "source_speaker_id": cand.speaker_id,
                    "voiceprint_id": cand.voiceprint_id,
                    "recording_id": recording_id,
                },
                after={
                    "display_name": node.display_name,
                    "role": node.speaker_role,
                    "strategy": node.merge_strategy,
                },
            )

        return SpeakerLinkReport(
            recording_id=recording_id,
            new_speakers=new_count,
            merged_speakers=merge_count,
            ambiguous_merges=ambiguous,
            fuzzy_merges=fuzzy_count,
            audit_written=audit_written,
            m9_pending_reconfirm=pending_reconfirm,
        )

    async def link_speakers(
        self,
        tenant_id: str,
    ) -> list[SpeakerLinkReport]:
        """Removed — batch backfill lives in ``core.voiceprint_backfill``.

        This was an empty stub whose docstring claimed a nightly cron called
        it and that scheduler.py wired it up; neither was ever true. Batch
        work cannot live here anyway: recordings that predate the voiceprint
        pipeline were never diarized, so backfilling them means re-running
        diarization, and ``SpeakerLinker`` has no voiceprint adapter.

        Raises:
            NotImplementedError: always. Use ``VoiceprintBackfill``.
        """
        raise NotImplementedError(
            "SpeakerLinker.link_speakers() was a no-op stub and has been removed. "
            "Use audio_graphy.core.voiceprint_backfill.VoiceprintBackfill "
            f"(tenant_id={tenant_id!r}), or scripts/backfill_voiceprints.py."
        )

    # ------------------------------------------------------------------
    # Layer 2 — fuzzy name matching (M9 R1 T12 / L8 ruling)
    # ------------------------------------------------------------------
    async def _try_layer2_fuzzy(
        self,
        candidate: _NewSpeakerCandidate,
        existing: list[SpeakerNode],
        recording_id: int,
    ) -> tuple[SpeakerNode, str, float, float | None] | None:
        """Run SpeakerFuzzyMatcher against existing speakers' display names.

        L8 decision tree:
          - Any fuzzy hit → enqueue a ``PENDING_REVIEW`` observation without
            mutating canonical speaker/vector/link state.
          - NO_MATCH   → return None (caller falls through to Layer 3).

        Returns:
            ``(node, ambiguity_tag, fuzzy_score, voiceprint_score)`` on hit,
            ``None`` on NO_MATCH.
        """
        from audio_graphy.core.speaker_fuzzy_matcher import (
            SpeakerCandidate as FuzzyCandidate,
        )
        from audio_graphy.core.speaker_fuzzy_matcher import (
            SpeakerFuzzyMatcher,
        )

        if not existing:
            return None

        matcher = self._fuzzy_matcher or SpeakerFuzzyMatcher()
        # Build fuzzy candidates from existing SpeakerNodes.
        fuzzy_candidates: list[FuzzyCandidate] = []
        for sn in existing:
            # Decrypt the voiceprint once (cached on the node).
            vec = self._get_cached_decrypted_vector(sn)
            fuzzy_candidates.append(
                FuzzyCandidate(
                    speaker_node_id=sn.id,
                    canonical_name=sn.display_name,
                    voiceprint_vector=tuple(vec) if vec is not None else None,
                )
            )

        # Derive the query name from the candidate (use role_hint as fallback).
        query_name = self._derive_query_name(candidate)
        if not query_name:
            return None

        result = matcher.match(
            query_name=query_name,
            candidates=fuzzy_candidates,
            query_voiceprint=candidate.voiceprint,
        )

        matched_candidate = result.matched_candidate
        if result.verdict == "NO_MATCH" or matched_candidate is None:
            return None

        # Find the matching SpeakerNode ORM by id.
        matched_sn = next(
            (sn for sn in existing if sn.id == matched_candidate.speaker_node_id),
            None,
        )
        if matched_sn is None:
            return None

        # Even a fuzzy result supported by a voiceprint stays staged: fuzzy
        # identity linkage is a human-review policy boundary, not a confidence
        # threshold.  Confirmation is the only path that may mutate canonical
        # state.
        enqueued = await self._enqueue_reconfirm(
            recording_id=recording_id,
            candidate_name=query_name,
            matched_node=matched_sn,
            fuzzy_score=result.fuzzy_score,
            voiceprint_score=result.voiceprint_score,
            candidate=candidate,
        )
        if not enqueued:
            return None

        return (
            matched_sn,
            "PENDING_REVIEW",
            result.fuzzy_score,
            result.voiceprint_score,
        )

    @staticmethod
    def _derive_query_name(candidate: _NewSpeakerCandidate) -> str:
        """Heuristically derive a display name from the candidate.

        The M7 diarization speaker_id (e.g. ``spk_0``) is not a usable
        display name for fuzzy matching. We prefer (in order):
          1. ``candidate.display_name`` (M9 T12 — populated when ASR
             transcribes a self-introduction like "我是王小姐").
          2. ``candidate.role_hint`` (``agent`` / ``customer``).
          3. ``candidate.speaker_id`` (last resort — usually NO_MATCH).
        """
        return candidate.display_name or candidate.role_hint or candidate.speaker_id

    async def _enqueue_reconfirm(
        self,
        *,
        recording_id: int,
        candidate_name: str,
        matched_node: SpeakerNode,
        fuzzy_score: float,
        voiceprint_score: float | None,
        candidate: _NewSpeakerCandidate | None = None,
    ) -> bool:
        """Insert a SpeakerMergePending row for human/voiceprint reconfirm.

        Best-effort: any DB failure is logged + swallowed so that Layer 2
        matches still complete. The audit trail captures the failure.
        """
        from audio_graphy.models.speaker_merge_pending import SpeakerMergePending

        vector_encrypted: bytes | None = None
        encryption_meta: dict[str, Any] | None = None
        if candidate is not None and self._crypto is not None:
            try:
                plaintext = struct.pack(
                    f"<{len(candidate.voiceprint)}f",
                    *[float(x) for x in candidate.voiceprint],
                )
                vector_encrypted, encryption_meta = self._crypto.encrypt_bytes(
                    plaintext,
                    context=f"voiceprint:{candidate.voiceprint_id}",
                )
            except Exception as exc:
                logger.warning(
                    "Speaker pending payload encryption failed (recording_id=%s, candidate=%s): %s",
                    recording_id,
                    candidate_name,
                    exc,
                )
                return False

        try:
            async with self._session_factory() as session:
                if candidate is not None:
                    existing_stmt = select(SpeakerMergePending.id).where(
                        SpeakerMergePending.tenant_id == self._tenant_id,
                        SpeakerMergePending.recording_id == recording_id,
                        SpeakerMergePending.candidate_speaker_id == candidate.speaker_id,
                        SpeakerMergePending.matched_speaker_node_id == matched_node.id,
                        SpeakerMergePending.status == "pending",
                    )
                    if (await session.execute(existing_stmt)).scalar_one_or_none() is not None:
                        return True
                row = SpeakerMergePending(
                    tenant_id=self._tenant_id,
                    recording_id=recording_id,
                    candidate_name=candidate_name,
                    matched_speaker_node_id=matched_node.id,
                    fuzzy_score=fuzzy_score,
                    status="pending",
                    voiceprint_score=voiceprint_score,
                    observation_state="PENDING_REVIEW",
                    candidate_speaker_id=(candidate.speaker_id if candidate is not None else None),
                    candidate_voiceprint_id=(
                        candidate.voiceprint_id if candidate is not None else None
                    ),
                    candidate_vector_encrypted=vector_encrypted,
                    candidate_encryption_meta=encryption_meta,
                    candidate_speech_sec=(candidate.speech_sec if candidate is not None else None),
                    candidate_sampled_sec=(
                        candidate.sampled_sec if candidate is not None else None
                    ),
                    candidate_first_seen=(candidate.first_seen if candidate is not None else None),
                    candidate_role_hint=(candidate.role_hint if candidate is not None else None),
                )
                session.add(row)
                await session.commit()
                return True
        except Exception as exc:
            logger.warning(
                "SpeakerMergePending insert failed (recording_id=%s, candidate=%s): %s",
                recording_id,
                candidate_name,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Layer 1 — voiceprint cosine matching
    # ------------------------------------------------------------------
    def _best_voiceprint_match(
        self,
        candidate: _NewSpeakerCandidate,
        existing: list[SpeakerNode],
    ) -> tuple[SpeakerNode, float, str | None] | None:
        """Find the highest-cosine existing speaker above threshold.

        Returns:
            ``(node, cosine, ambiguity_tag)`` if matched, else ``None``.
            ``ambiguity_tag`` is ``None`` for cos ≥ ambiguity_threshold,
            ``"AMBIGUOUS"`` otherwise.
        """
        if not existing:
            return None

        best_node: SpeakerNode | None = None
        best_cos = -1.0

        for node in existing:
            # Decrypt existing voiceprint lazily — but this requires DB hit.
            # SpeakerLinker keeps decrypted vectors in node.attrs["_decrypted_vec"]
            # cache after first decrypt to avoid repeat work within one run.
            vec = self._get_cached_decrypted_vector(node)
            if vec is None:
                continue
            cos = _cosine(candidate.voiceprint, vec)
            if cos > best_cos:
                best_cos = cos
                best_node = node

        if best_node is None or best_cos < self._vp_threshold:
            return None

        ambiguity_tag = None if best_cos >= self._ambiguity_threshold else "AMBIGUOUS"
        return best_node, best_cos, ambiguity_tag

    def _get_cached_decrypted_vector(self, node: SpeakerNode) -> tuple[float, ...] | None:
        """Return the cached decrypted voiceprint for a SpeakerNode, if any.

        SpeakerLinker loaders / creators populate the non-persisted
        ``node._runtime_vec`` attribute (a tuple of floats) when they
        decrypt or construct the voiceprint. When absent (e.g. node was
        inserted by external code without going through the linker),
        return ``None`` so the candidate cannot match against it.
        """
        cached = getattr(node, "_runtime_vec", None)
        if isinstance(cached, (tuple, list)):
            return tuple(cached)
        return None

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    async def _load_existing_speakers(self) -> list[SpeakerNode]:
        """Load tenant speakers and each speaker's latest voiceprint in two queries."""
        async with self._session_factory() as session:
            stmt = select(SpeakerNode).where(SpeakerNode.tenant_id == self._tenant_id)
            result = await session.execute(stmt)
            nodes = list(result.scalars().all())

            if not nodes:
                return []

            # Representative template selection (ADR-0001), in priority order:
            #   1. Confidently attached rows first. A tentative (AMBIGUOUS)
            #      merge must never redefine the speaker it merged into.
            #   2. Then the longest sample — embedding quality rises with
            #      speech duration, so it is the most trustworthy template.
            #      Newest-wins, the old rule, let one short low-confidence
            #      merge hijack the speaker for every later comparison.
            # Every row is still retained for audit/DSAR; only the choice of
            # template changes. A node always has at least one confident row:
            # the sample it was created from.
            confidently_attached = VoiceprintVector.attach_cosine >= self._ambiguity_threshold
            ranked_voiceprints = (
                select(
                    VoiceprintVector.id.label("voiceprint_row_id"),
                    func.row_number()
                    .over(
                        partition_by=VoiceprintVector.speaker_entity_id,
                        order_by=(
                            confidently_attached.desc(),
                            VoiceprintVector.duration_sec.desc(),
                            VoiceprintVector.created_at.desc(),
                            VoiceprintVector.id.desc(),
                        ),
                    )
                    .label("row_number"),
                )
                .where(VoiceprintVector.tenant_id == self._tenant_id)
                .subquery()
            )
            latest_stmt = (
                select(VoiceprintVector)
                .join(
                    ranked_voiceprints,
                    VoiceprintVector.id == ranked_voiceprints.c.voiceprint_row_id,
                )
                .where(
                    ranked_voiceprints.c.row_number == 1,
                    VoiceprintVector.tenant_id == self._tenant_id,
                )
            )
            latest_result = await session.execute(latest_stmt)
            latest_by_speaker = {
                row.speaker_entity_id: row for row in latest_result.scalars().all()
            }

        # Decrypt outside the session: all encrypted payload columns are loaded,
        # and the runtime vectors must never be persisted back onto ORM rows.
        for node in nodes:
            vp_row = latest_by_speaker.get(node.id)
            try:
                if vp_row is not None:
                    decrypted = vp_row.decrypted_vector(self._crypto)
                    # Use a non-persisted attribute to avoid triggering updates.
                    object.__setattr__(node, "_runtime_vec", tuple(float(x) for x in decrypted))
                else:
                    object.__setattr__(node, "_runtime_vec", None)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt voiceprint for speaker_node %d: %s",
                    node.id,
                    exc,
                )
                object.__setattr__(node, "_runtime_vec", None)
        return nodes

    async def _merge_into_existing(
        self,
        candidate: _NewSpeakerCandidate,
        node: SpeakerNode,
        cosine: float,
        ambiguity_tag: str | None,
        recording_id: int,
    ) -> None:
        """Merge ``candidate`` into ``node``: persist voiceprint + link + update node."""
        # Persist encrypted voiceprint row, tagged with the cosine that
        # justified the merge so a tentative match cannot later be picked as
        # this speaker's representative template.
        await self._persist_voiceprint(candidate, node.id, attach_cosine=cosine)

        # Update node aggregations.
        recordings_list = list(node.recordings_list or [])
        if recording_id not in recordings_list:
            recordings_list.append(recording_id)

        async with self._session_factory() as session:
            db_node = await session.get(SpeakerNode, node.id)
            if db_node is None:
                logger.warning("SpeakerNode %d vanished mid-merge", node.id)
                return
            db_node.recordings_list = recordings_list
            db_node.recordings_count = len(recordings_list)
            # Accumulate onto the row just read in this transaction, not the
            # snapshot loaded at run() start: diarization routinely splits one
            # person into spk_0/spk_1, and both candidates can merge into the
            # same node — a stale base silently discards the first one's speech.
            db_node.total_speech_sec = float(db_node.total_speech_sec or 0.0) + candidate.speech_sec
            if db_node.first_seen is None or (
                candidate.first_seen is not None
                and _as_utc(candidate.first_seen) < _as_utc(db_node.first_seen)
            ):
                db_node.first_seen = candidate.first_seen
            db_node.merge_confidence = max(float(db_node.merge_confidence or 0.0), cosine)
            db_node.merge_strategy = "voiceprint"
            db_node.ambiguity_tag = ambiguity_tag
            await session.commit()

        # Insert SpeakerLink audit row.
        await self._persist_speaker_link(
            canonical_id=node.id,
            source_id=node.id,  # source_id == canonical for merges (no separate node)
            recording_id=recording_id,
            cosine=cosine,
            confidence=cosine,
            strategy="voiceprint",
            ambiguity_tag=ambiguity_tag,
            source_speaker_label=candidate.speaker_id,
        )

    async def _create_new_speaker(
        self,
        candidate: _NewSpeakerCandidate,
        recording_id: int,
    ) -> SpeakerNode:
        """Create a brand-new SpeakerNode + voiceprint row + speaker_link."""
        display_name = f"speaker:vp_{candidate.voiceprint_id[:8]}"
        async with self._session_factory() as session:
            node = SpeakerNode(
                tenant_id=self._tenant_id,
                voiceprint_id=candidate.voiceprint_id,
                display_name=display_name,
                speaker_role=candidate.role_hint,
                recordings_list=[recording_id],
                recordings_count=1,
                first_seen=candidate.first_seen,
                total_speech_sec=candidate.speech_sec,
                merge_confidence=1.0,
                merge_strategy="single_recording",
                ambiguity_tag=None,
                attrs={},
            )
            session.add(node)
            await session.flush()  # populate node.id
            node_id = node.id
            await session.commit()

        # Re-fetch for return (so caller has a fresh instance).
        async with self._session_factory() as session:
            fresh = await session.get(SpeakerNode, node_id)
            assert fresh is not None

        # Persist voiceprint row.
        await self._persist_voiceprint(candidate, node_id)

        # SpeakerLink audit row.
        await self._persist_speaker_link(
            canonical_id=node_id,
            source_id=node_id,
            recording_id=recording_id,
            cosine=None,
            confidence=1.0,
            strategy="single_recording",
            ambiguity_tag=None,
            source_speaker_label=candidate.speaker_id,
        )

        # Stash decrypted vec so subsequent candidates in this run can match.
        object.__setattr__(fresh, "_runtime_vec", candidate.voiceprint)
        return fresh

    async def _persist_voiceprint(
        self,
        candidate: _NewSpeakerCandidate,
        speaker_node_id: int,
        *,
        attach_cosine: float = 1.0,
    ) -> None:
        """Encrypt + insert VoiceprintVector row.

        ``attach_cosine`` records how confidently this vector belongs to
        ``speaker_node_id``: 1.0 when the vector defines the speaker, or the
        matching cosine when it was merged in. Rows below the ambiguity
        threshold are kept for audit but never chosen as the speaker's
        representative template (ADR-0001).
        """
        # Pack floats as little-endian float32 bytes (one 4-byte word per dim).
        # ``hash_voiceprint`` uses the same packing for the voiceprint_id hash,
        # so a re-decrypt + re-hash will match the candidate's voiceprint_id.
        plaintext = struct.pack(
            f"<{len(candidate.voiceprint)}f", *[float(x) for x in candidate.voiceprint]
        )
        ciphertext, meta = self._crypto.encrypt_bytes(
            plaintext, context=f"voiceprint:{candidate.voiceprint_id}"
        )
        async with self._session_factory() as session:
            # ``ux_vp_voiceprint_id`` is unique per (tenant, voiceprint_id),
            # and sampling is deterministic — a partially-completed earlier
            # run leaves rows that would make this insert raise and abort the
            # remaining candidates. Treat an existing row as done.
            duplicate = (
                await session.execute(
                    select(VoiceprintVector.id).where(
                        VoiceprintVector.tenant_id == self._tenant_id,
                        VoiceprintVector.voiceprint_id == candidate.voiceprint_id,
                    )
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                logger.info(
                    "Voiceprint %s already stored for tenant %s; skipping insert",
                    candidate.voiceprint_id[:12],
                    self._tenant_id,
                )
                return
            row = VoiceprintVector(
                tenant_id=self._tenant_id,
                recording_id=candidate.recording_id,
                segment_id=None,
                speaker_entity_id=speaker_node_id,
                voiceprint_id=candidate.voiceprint_id,
                vector_encrypted=ciphertext,
                encryption_meta=meta,
                # The audio behind this vector, not the speaker's total
                # speech — this column ranks template quality (ADR-0001).
                # Legacy callers that never set sampled_sec fall back to
                # speech_sec, which is what M7 stored.
                duration_sec=(candidate.sampled_sec or candidate.speech_sec),
                attach_cosine=attach_cosine,
            )
            session.add(row)
            await session.commit()

    async def _persist_speaker_link(
        self,
        *,
        canonical_id: int,
        source_id: int,
        recording_id: int,
        cosine: float | None,
        confidence: float,
        strategy: str,
        ambiguity_tag: str | None,
        source_speaker_label: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            link = SpeakerLink(
                tenant_id=self._tenant_id,
                canonical_speaker_id=canonical_id,
                source_speaker_id=source_id,
                recording_id=recording_id,
                cosine_similarity=cosine,
                merge_confidence=confidence,
                strategy=strategy,
                ambiguity_tag=ambiguity_tag,
                source_speaker_label=source_speaker_label,
            )
            session.add(link)
            await session.commit()

    async def _write_audit(
        self,
        *,
        action: str,
        target: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> int:
        """Write one audit row. Returns 1 if written, 0 if writer is None."""
        if self._audit is None:
            return 0
        await self._audit.record(
            tenant_id=self._tenant_id,
            user_id=None,
            action=action,
            target=target,
            before=before,
            after=after,
        )
        return 1


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _as_utc(value: datetime) -> datetime:
    """Make a timestamp comparable regardless of where it came from.

    MySQL DATETIME columns come back naive even when the ORM declares
    ``timezone=True``, while candidates carry tz-aware timestamps — comparing
    them directly raises TypeError and aborts the merge. Everything we store
    is UTC, so a naive value is read as UTC.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity for equal-length L2-normalized vectors.

    Falls back to the full formula (not just dot product) so un-normalized
    inputs still produce a valid score.
    """
    if len(a) != len(b) or not a:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a < 1e-12 or norm_b < 1e-12:
        return -1.0
    return dot / (norm_a * norm_b)


def _float_to_uint32(x: float) -> bytes:
    """Pack a float32 as little-endian uint32 bytes via struct.

    Retained for backwards compatibility — M7 caller code now uses
    ``struct.pack`` directly in ``_persist_voiceprint``.
    """
    return struct.pack("<f", float(x))


def hash_voiceprint(vector: Sequence[float]) -> str:
    """sha256 of the float32-packed voiceprint vector — the ``voiceprint_id``.

    Used by both SpeakerLinker (when constructing candidates) and the
    DSAR / retention cascade (to verify hash matches row).
    """
    payload = struct.pack(f"<{len(vector)}f", *[float(x) for x in vector])
    return hashlib.sha256(payload).hexdigest()


def derive_role_hint(
    segments_for_recording: Sequence[tuple[str, float]],
) -> dict[str, str]:
    """Derive a per-speaker role hint via the §17.9 heuristic.

    Args:
        segments_for_recording: ``(speaker_id, duration_sec)`` tuples.

    Returns:
        Mapping ``speaker_id → role`` where role is ``agent`` / ``customer``
        / ``unknown``. Single-speaker recordings get ``unknown``; multi-speaker
        recordings pick the longest-talker as ``agent`` and the rest as
        ``customer`` (only when ≥ 60% of total); 3+ speakers default to
        ``unknown``.
    """
    if not segments_for_recording:
        return {}

    by_speaker: dict[str, float] = {}
    for spk, dur in segments_for_recording:
        by_speaker[spk] = by_speaker.get(spk, 0.0) + dur

    if len(by_speaker) == 1:
        return {next(iter(by_speaker)): "unknown"}
    if len(by_speaker) >= 3:
        return dict.fromkeys(by_speaker, "unknown")

    # Two speakers — longer → agent (only if ≥ 60% of total).
    total = sum(by_speaker.values())
    if total <= 0:
        return dict.fromkeys(by_speaker, "unknown")

    items = sorted(by_speaker.items(), key=lambda kv: kv[1], reverse=True)
    longer_id, longer_dur = items[0]
    shorter_id, _ = items[1]
    if longer_dur / total >= 0.6:
        return {longer_id: "agent", shorter_id: "customer"}
    return {longer_id: "unknown", shorter_id: "unknown"}


__all__ = [
    "SpeakerLinkReport",
    "SpeakerLinker",
    "derive_role_hint",
    "hash_voiceprint",
]
