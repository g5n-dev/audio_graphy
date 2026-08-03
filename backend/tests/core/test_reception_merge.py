"""Reception grouping tests for fragmented and long-form retail recordings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from audio_graphy.core.reception_merge import (
    ManualReceptionConstraints,
    ReceptionMerger,
    ReceptionProposal,
    ReceptionTurn,
    RecordingCandidate,
)

BASE_TIME = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)


def _recording(
    recording_id: str,
    minute: float,
    *,
    duration_min: float = 2,
    tenant: str = "tenant-a",
    store: str = "store-1",
    agent: str | None = "agent-1",
    customer: str | None = None,
    embedding: tuple[float, ...] | None = None,
    reception_id: str | None = None,
    session_id: str | None = None,
) -> RecordingCandidate:
    started_at = BASE_TIME + timedelta(minutes=minute)
    return RecordingCandidate(
        recording_id=recording_id,
        tenant_id=tenant,
        store_id=store,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=duration_min),
        agent_id=agent,
        customer_hash_claim=customer,
        semantic_embedding=embedding,
        explicit_reception_id=reception_id,
        explicit_session_id=session_id,
    )


class TestExplicitReceptionIdentity:
    def test_same_explicit_reception_id_has_highest_automatic_priority(self) -> None:
        left = _recording(
            "a",
            0,
            customer="customer-1",
            reception_id="reception-42",
        )
        right = _recording(
            "b",
            40,
            customer="voiceprint-noise",
            reception_id="reception-42",
        )

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "merge"
        assert proposal.confidence >= 0.98
        assert "explicit_reception_match" in proposal.reason_codes

    def test_same_explicit_session_id_merges_short_stream_fragments(self) -> None:
        left = _recording("a", 0, session_id="stream-session")
        right = _recording("b", 18, session_id="stream-session")

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "merge"
        assert "explicit_session_match" in proposal.reason_codes

    def test_conflicting_explicit_reception_ids_reject(self) -> None:
        left = _recording("a", 0, reception_id="r-1")
        right = _recording("b", 1, reception_id="r-2")

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "reject"
        assert "explicit_reception_conflict" in proposal.reason_codes


class TestSafetyInvariants:
    def test_cross_tenant_never_merges_even_with_same_explicit_id(self) -> None:
        left = _recording("a", 0, tenant="tenant-a", reception_id="r-1")
        right = _recording("b", 1, tenant="tenant-b", reception_id="r-1")

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "reject"
        assert "tenant_mismatch" in proposal.reason_codes

    def test_cross_store_does_not_auto_merge(self) -> None:
        left = _recording("a", 0, store="store-1", customer="customer-1")
        right = _recording("b", 1, store="store-2", customer="customer-1")

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "reject"
        assert "store_mismatch" in proposal.reason_codes

    def test_conflicting_customer_voiceprints_reject_without_explicit_identity(self) -> None:
        left = _recording("a", 0, customer="customer-1", embedding=(1.0, 0.0))
        right = _recording("b", 1, customer="customer-2", embedding=(1.0, 0.0))

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "reject"
        assert "customer_voiceprint_conflict" in proposal.reason_codes

    def test_single_weak_feature_never_auto_merges_across_unknown_customers(self) -> None:
        left = _recording("a", 0, agent="agent-1")
        right = _recording("b", 3, agent="agent-1")

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "needs_review"
        assert "same_agent" in proposal.reason_codes

    def test_time_proximity_alone_is_not_enough(self) -> None:
        left = _recording("a", 0, agent=None)
        right = _recording("b", 1, agent=None)

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision != "merge"

    def test_recordings_beyond_default_fifteen_minutes_reject(self) -> None:
        left = _recording(
            "a",
            0,
            duration_min=1,
            customer="customer-1",
            embedding=(1.0, 0.0),
        )
        right = _recording(
            "b",
            17,
            customer="customer-1",
            embedding=(1.0, 0.0),
        )

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "reject"
        assert "merge_window_exceeded" in proposal.reason_codes

    def test_unexplained_time_overlap_requires_review(self) -> None:
        left = _recording("a", 0, duration_min=10, customer="customer-1")
        right = _recording("b", 5, duration_min=3, customer="customer-1")

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "needs_review"
        assert "time_overlap" in proposal.reason_codes


class TestAutomaticMergeEvidence:
    def test_same_customer_and_time_continuity_auto_merge(self) -> None:
        left = _recording("a", 0, customer="customer-1")
        right = _recording("b", 4, customer="customer-1")

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "merge"
        assert {"same_customer_voiceprint", "within_merge_window"} <= set(proposal.reason_codes)

    def test_agent_semantic_and_time_continuity_auto_merge_without_voiceprint(self) -> None:
        left = _recording("a", 0, embedding=(1.0, 0.0))
        right = _recording("b", 4, embedding=(0.98, 0.02))

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision == "merge"
        assert {"same_agent", "semantic_continuity", "within_merge_window"} <= set(
            proposal.reason_codes
        )

    def test_low_semantic_continuity_does_not_auto_merge(self) -> None:
        left = _recording("a", 0, embedding=(1.0, 0.0))
        right = _recording("b", 4, embedding=(0.0, 1.0))

        proposal = ReceptionMerger().evaluate_pair(left, right)

        assert proposal.decision != "merge"
        assert "semantic_discontinuity" in proposal.reason_codes


class TestMultiRecordingGrouping:
    def test_dense_500_recording_window_has_linear_comparison_bound(self) -> None:
        class CountingMerger(ReceptionMerger):
            evaluation_count = 0

            def evaluate_pair(
                self,
                left: RecordingCandidate,
                right: RecordingCandidate,
                *,
                constraints: ManualReceptionConstraints | None = None,
            ) -> ReceptionProposal:
                self.evaluation_count += 1
                return super().evaluate_pair(left, right, constraints=constraints)

        recordings = [_recording(str(index), 0, duration_min=1, agent=None) for index in range(500)]
        merger = CountingMerger()

        proposals = merger.propose_groups(recordings, max_neighbors=16)

        assert merger.evaluation_count <= len(recordings) * 16
        assert len(proposals) <= len(recordings) * 16

    def test_multiple_short_recordings_form_independent_reception_groups(self) -> None:
        recordings = [
            _recording("a-2", 4, customer="customer-a"),
            _recording("b-2", 34, reception_id="reception-b"),
            _recording("a-1", 0, customer="customer-a"),
            _recording("b-1", 30, reception_id="reception-b"),
        ]

        proposals = ReceptionMerger().propose_groups(recordings)
        merged = {
            frozenset(proposal.recording_ids)
            for proposal in proposals
            if proposal.decision == "merge"
        }

        assert merged == {frozenset({"a-1", "a-2"}), frozenset({"b-1", "b-2"})}

    def test_input_time_order_does_not_change_grouping(self) -> None:
        recordings = [
            _recording("third", 8, customer="customer-a"),
            _recording("first", 0, customer="customer-a"),
            _recording("second", 4, customer="customer-a"),
        ]

        proposals = ReceptionMerger().propose_groups(recordings)

        assert len(proposals) == 1
        assert proposals[0].decision == "merge"
        assert proposals[0].recording_ids == ("first", "second", "third")

    def test_grouping_does_not_bridge_two_known_customers_transitively(self) -> None:
        recordings = [
            _recording("a", 0, customer="customer-a"),
            _recording("unknown", 3, customer=None, embedding=(1.0, 0.0)),
            _recording("b", 6, customer="customer-b", embedding=(1.0, 0.0)),
        ]

        proposals = ReceptionMerger().propose_groups(recordings)

        assert not any(
            {"a", "b"} <= set(proposal.recording_ids) and proposal.decision == "merge"
            for proposal in proposals
        )

    def test_unknown_fragment_does_not_bridge_conflicting_explicit_receptions(self) -> None:
        recordings = [
            _recording(
                "r1",
                0,
                customer="same-customer",
                reception_id="reception-1",
            ),
            _recording("unknown", 3, customer="same-customer"),
            _recording(
                "r2",
                6,
                customer="same-customer",
                reception_id="reception-2",
            ),
        ]

        proposals = ReceptionMerger().propose_groups(recordings)

        assert not any(
            {"r1", "r2"} <= set(proposal.recording_ids) and proposal.decision == "merge"
            for proposal in proposals
        )


class TestManualConstraints:
    def test_force_split_blocks_transitive_component_union(self) -> None:
        recordings = [
            _recording("a", 0, customer="same"),
            _recording("b", 3, customer="same"),
            _recording("c", 6, customer="same"),
        ]
        constraints = ManualReceptionConstraints.from_pairs(
            force_split=[("a", "c")],
        )

        proposals = ReceptionMerger().propose_groups(
            recordings,
            constraints=constraints,
        )

        assert not any(
            {"a", "c"} <= set(proposal.recording_ids) and proposal.decision == "merge"
            for proposal in proposals
        )

    def test_force_split_wins_over_matching_explicit_identity(self) -> None:
        left = _recording("a", 0, reception_id="r-1")
        right = _recording("b", 1, reception_id="r-1")
        constraints = ManualReceptionConstraints.from_pairs(force_split=[("a", "b")])

        proposal = ReceptionMerger().evaluate_pair(left, right, constraints=constraints)

        assert proposal.decision == "reject"
        assert "manual_force_split" in proposal.reason_codes

    def test_force_merge_can_resolve_review_with_auditable_reason(self) -> None:
        left = _recording("a", 0, agent=None)
        right = _recording("b", 3, agent=None)
        constraints = ManualReceptionConstraints.from_pairs(force_merge=[("a", "b")])

        proposal = ReceptionMerger().evaluate_pair(left, right, constraints=constraints)

        assert proposal.decision == "merge"
        assert proposal.manual_override is True
        assert "manual_force_merge" in proposal.reason_codes


class TestLongRecordingReceptionSplits:
    def test_split_signal_collection_has_a_hard_limit(self) -> None:
        turns = [
            ReceptionTurn(
                segment_id=str(index),
                start_sec=float(index * 60),
                end_sec=float(index * 60 + 5),
                transcript="您好，欢迎光临",
            )
            for index in range(100)
        ]

        signals = ReceptionMerger().detect_recording_splits(turns, max_signals=5)

        assert len(signals) == 5

    def test_regreeting_long_pause_and_customer_change_emit_split_signal(self) -> None:
        turns = [
            ReceptionTurn(
                segment_id="s1",
                start_sec=0,
                end_sec=10,
                transcript="感谢光临慢走",
                speaker="agent",
                customer_hash_claim="customer-a",
            ),
            ReceptionTurn(
                segment_id="s2",
                start_sec=75,
                end_sec=80,
                transcript="您好，欢迎光临，今天想看什么",
                speaker="agent",
                customer_hash_claim="customer-b",
            ),
        ]

        signals = ReceptionMerger().detect_recording_splits(turns)

        assert len(signals) == 1
        assert signals[0].at_segment_id == "s2"
        assert {
            "re_greeting",
            "long_pause",
            "customer_change",
        } <= set(signals[0].reason_codes)

    def test_single_regreeting_signal_does_not_split_long_recording(self) -> None:
        turns = [
            ReceptionTurn(
                segment_id="s1",
                start_sec=0,
                end_sec=10,
                transcript="我再介绍一下",
                speaker="agent",
                customer_hash_claim="customer-a",
            ),
            ReceptionTurn(
                segment_id="s2",
                start_sec=12,
                end_sec=18,
                transcript="您好，我重新给您介绍这款",
                speaker="agent",
                customer_hash_claim="customer-a",
            ),
        ]

        assert ReceptionMerger().detect_recording_splits(turns) == []

    def test_turn_time_order_is_normalized_before_split_detection(self) -> None:
        turns = [
            ReceptionTurn(
                segment_id="later",
                start_sec=90,
                end_sec=95,
                transcript="您好欢迎光临",
                speaker="agent",
                customer_hash_claim="customer-b",
            ),
            ReceptionTurn(
                segment_id="earlier",
                start_sec=0,
                end_sec=5,
                transcript="慢走",
                speaker="agent",
                customer_hash_claim="customer-a",
            ),
        ]

        signals = ReceptionMerger().detect_recording_splits(turns)

        assert len(signals) == 1
        assert signals[0].at_segment_id == "later"
