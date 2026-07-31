"""Pure reception grouping for fragmented retail audio.

The grouping policy is conservative by design.  Tenant and store boundaries
are hard automatic constraints, explicit reception/session identifiers have
the highest positive priority, and weak proximity evidence never auto-merges
unknown customers by itself.

The module also detects a second reception inside one long recording using
multiple independent signals (re-greeting, long pause, customer change).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Literal

ReceptionDecision = Literal["merge", "reject", "needs_review"]


@dataclass(frozen=True, slots=True)
class RecordingCandidate:
    """Metadata and optional model features for one source recording."""

    recording_id: str
    tenant_id: str
    store_id: str
    started_at: datetime
    ended_at: datetime
    agent_id: str | None = None
    customer_voiceprint_id: str | None = None
    semantic_embedding: tuple[float, ...] | None = None
    explicit_reception_id: str | None = None
    explicit_session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.recording_id:
            raise ValueError("recording_id must not be empty")
        if not self.tenant_id:
            raise ValueError("tenant_id must not be empty")
        if not self.store_id:
            raise ValueError("store_id must not be empty")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must be greater than or equal to started_at")


@dataclass(frozen=True, slots=True)
class MergeFeatureReason:
    """Auditable feature contribution to a grouping decision."""

    code: str
    contribution: float
    detail: str
    hard_constraint: bool = False


@dataclass(frozen=True, slots=True)
class ReceptionProposal:
    """One merge/reject/review proposal over one or more recordings."""

    recording_ids: tuple[str, ...]
    decision: ReceptionDecision
    confidence: float
    reasons: tuple[MergeFeatureReason, ...]
    manual_override: bool = False

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Convenient stable projection for UI, analytics, and tests."""
        return tuple(reason.code for reason in self.reasons)


@dataclass(frozen=True, slots=True)
class ManualReceptionConstraints:
    """Auditable human merge/split constraints, expressed as unordered pairs."""

    force_merge: frozenset[frozenset[str]] = field(default_factory=frozenset)
    force_split: frozenset[frozenset[str]] = field(default_factory=frozenset)

    @classmethod
    def from_pairs(
        cls,
        *,
        force_merge: Iterable[tuple[str, str]] = (),
        force_split: Iterable[tuple[str, str]] = (),
    ) -> ManualReceptionConstraints:
        """Normalize pairs so caller ordering cannot change the result."""

        def normalize(pairs: Iterable[tuple[str, str]]) -> frozenset[frozenset[str]]:
            normalized: set[frozenset[str]] = set()
            for left, right in pairs:
                if not left or not right or left == right:
                    raise ValueError("manual constraint pairs require two distinct IDs")
                normalized.add(frozenset((left, right)))
            return frozenset(normalized)

        merged = normalize(force_merge)
        split = normalize(force_split)
        if merged & split:
            raise ValueError("the same pair cannot be force-merged and force-split")
        return cls(force_merge=merged, force_split=split)

    def pair_mode(self, left_id: str, right_id: str) -> Literal["merge", "split"] | None:
        pair = frozenset((left_id, right_id))
        if pair in self.force_split:
            return "split"
        if pair in self.force_merge:
            return "merge"
        return None


@dataclass(frozen=True, slots=True)
class ReceptionTurn:
    """Minimal long-recording turn used for reception split detection."""

    segment_id: str
    start_sec: float
    end_sec: float
    transcript: str
    speaker: str | None = None
    customer_voiceprint_id: str | None = None

    def __post_init__(self) -> None:
        if self.end_sec < self.start_sec:
            raise ValueError("end_sec must be greater than or equal to start_sec")


@dataclass(frozen=True, slots=True)
class ReceptionSplitSignal:
    """Evidence that a new reception begins inside a long recording."""

    at_segment_id: str
    at_sec: float
    confidence: float
    reasons: tuple[MergeFeatureReason, ...]

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(reason.code for reason in self.reasons)


class ReceptionMerger:
    """Conservative, explainable short-recording reception grouper."""

    _REGREETING_PHRASES = (
        "欢迎光临",
        "欢迎到店",
        "您好",
        "你好",
        "今天想看什么",
        "需要看点什么",
    )

    def __init__(
        self,
        *,
        merge_window: timedelta = timedelta(minutes=15),
        semantic_merge_threshold: float = 0.85,
        semantic_review_threshold: float = 0.65,
        long_recording_pause_sec: float = 45.0,
    ) -> None:
        if merge_window <= timedelta(0):
            raise ValueError("merge_window must be positive")
        if not 0 <= semantic_review_threshold <= semantic_merge_threshold <= 1:
            raise ValueError("semantic thresholds must satisfy 0 <= review <= merge <= 1")
        if long_recording_pause_sec <= 0:
            raise ValueError("long_recording_pause_sec must be positive")
        self.merge_window = merge_window
        self.semantic_merge_threshold = semantic_merge_threshold
        self.semantic_review_threshold = semantic_review_threshold
        self.long_recording_pause_sec = long_recording_pause_sec

    def evaluate_pair(
        self,
        left: RecordingCandidate,
        right: RecordingCandidate,
        *,
        constraints: ManualReceptionConstraints | None = None,
    ) -> ReceptionProposal:
        """Evaluate two fragments using hard constraints then weighted evidence."""
        if left.recording_id == right.recording_id:
            raise ValueError("cannot compare a recording with itself")
        first, second = sorted(
            (left, right),
            key=lambda item: (item.started_at, item.ended_at, item.recording_id),
        )
        manual_mode = (constraints or ManualReceptionConstraints()).pair_mode(
            first.recording_id,
            second.recording_id,
        )

        if manual_mode == "split":
            return self._proposal(
                first,
                second,
                "reject",
                1.0,
                MergeFeatureReason(
                    "manual_force_split",
                    -1.0,
                    "human constraint explicitly separates the recordings",
                    hard_constraint=True,
                ),
                manual_override=True,
            )

        if first.tenant_id != second.tenant_id:
            return self._proposal(
                first,
                second,
                "reject",
                1.0,
                MergeFeatureReason(
                    "tenant_mismatch",
                    -1.0,
                    f"{first.tenant_id}!={second.tenant_id}",
                    hard_constraint=True,
                ),
            )

        if manual_mode == "merge":
            manual_reasons = [
                MergeFeatureReason(
                    "manual_force_merge",
                    1.0,
                    "human constraint explicitly joins the recordings",
                    hard_constraint=True,
                )
            ]
            if first.store_id != second.store_id:
                manual_reasons.append(
                    MergeFeatureReason(
                        "store_mismatch_overridden",
                        -0.2,
                        f"{first.store_id}!={second.store_id}",
                    )
                )
            if self._known_customer_conflict(first, second):
                manual_reasons.append(
                    MergeFeatureReason(
                        "customer_conflict_overridden",
                        -0.3,
                        "different voiceprints retained in the audit trail",
                    )
                )
            return self._proposal(
                first,
                second,
                "merge",
                1.0,
                *manual_reasons,
                manual_override=True,
            )

        if first.store_id != second.store_id:
            return self._proposal(
                first,
                second,
                "reject",
                1.0,
                MergeFeatureReason(
                    "store_mismatch",
                    -1.0,
                    f"{first.store_id}!={second.store_id}",
                    hard_constraint=True,
                ),
            )

        explicit_reception = self._explicit_identity_decision(
            first,
            second,
            attribute="explicit_reception_id",
            match_code="explicit_reception_match",
            conflict_code="explicit_reception_conflict",
            match_confidence=0.995,
        )
        if explicit_reception is not None:
            return explicit_reception

        explicit_session = self._explicit_identity_decision(
            first,
            second,
            attribute="explicit_session_id",
            match_code="explicit_session_match",
            conflict_code="explicit_session_conflict",
            match_confidence=0.99,
        )
        if explicit_session is not None:
            return explicit_session

        if self._known_customer_conflict(first, second):
            return self._proposal(
                first,
                second,
                "reject",
                0.99,
                MergeFeatureReason(
                    "customer_voiceprint_conflict",
                    -1.0,
                    (f"{first.customer_voiceprint_id}!={second.customer_voiceprint_id}"),
                    hard_constraint=True,
                ),
            )

        overlap_sec = self._overlap_seconds(first, second)
        if overlap_sec > 0:
            return self._proposal(
                first,
                second,
                "needs_review",
                0.82,
                MergeFeatureReason(
                    "time_overlap",
                    -0.45,
                    f"overlap={overlap_sec:.1f}s",
                ),
                *self._identity_reasons(first, second),
            )

        gap = second.started_at - first.ended_at
        if gap > self.merge_window:
            return self._proposal(
                first,
                second,
                "reject",
                min(1.0, 0.75 + gap / (self.merge_window * 10)),
                MergeFeatureReason(
                    "merge_window_exceeded",
                    -1.0,
                    f"gap={gap.total_seconds():.1f}s",
                    hard_constraint=True,
                ),
            )

        reasons: list[MergeFeatureReason] = [
            MergeFeatureReason(
                "within_merge_window",
                0.20,
                f"gap={gap.total_seconds():.1f}s",
            )
        ]
        same_agent = bool(first.agent_id and first.agent_id == second.agent_id)
        same_customer = bool(
            first.customer_voiceprint_id
            and first.customer_voiceprint_id == second.customer_voiceprint_id
        )
        if same_agent:
            reasons.append(
                MergeFeatureReason(
                    "same_agent",
                    0.15,
                    f"agent={first.agent_id}",
                )
            )
        if same_customer:
            reasons.append(
                MergeFeatureReason(
                    "same_customer_voiceprint",
                    0.50,
                    f"voiceprint={first.customer_voiceprint_id}",
                )
            )

        similarity = self._cosine_similarity(
            first.semantic_embedding,
            second.semantic_embedding,
        )
        semantic_merge = similarity is not None and similarity >= self.semantic_merge_threshold
        if semantic_merge:
            reasons.append(
                MergeFeatureReason(
                    "semantic_continuity",
                    0.30,
                    f"cosine_similarity={similarity:.3f}",
                )
            )
        elif similarity is not None and similarity >= self.semantic_review_threshold:
            reasons.append(
                MergeFeatureReason(
                    "semantic_continuity_review",
                    0.15,
                    f"cosine_similarity={similarity:.3f}",
                )
            )
        elif similarity is not None:
            reasons.append(
                MergeFeatureReason(
                    "semantic_discontinuity",
                    -0.35,
                    f"cosine_similarity={similarity:.3f}",
                )
            )

        # A stable customer identity plus time is sufficient.  Without it,
        # require the independent triad of time + agent + strong semantics.
        if same_customer or (same_agent and semantic_merge):
            positive = sum(max(0.0, reason.contribution) for reason in reasons)
            return self._proposal(
                first,
                second,
                "merge",
                min(0.97, 0.58 + positive * 0.38),
                *reasons,
            )

        review_confidence = 0.48 + sum(max(0.0, reason.contribution) for reason in reasons) * 0.25
        return self._proposal(
            first,
            second,
            "needs_review",
            min(0.85, review_confidence),
            *reasons,
        )

    def propose_groups(
        self,
        recordings: Sequence[RecordingCandidate],
        *,
        constraints: ManualReceptionConstraints | None = None,
        max_neighbors: int | None = None,
    ) -> list[ReceptionProposal]:
        """Build merge components and unresolved review candidates.

        ``max_neighbors`` bounds dense automatic-discovery windows to O(n*k)
        pair evaluations.  Callers that need exhaustive/manual reconciliation
        can keep the default.
        """
        if max_neighbors is not None and max_neighbors <= 0:
            raise ValueError("max_neighbors must be positive")
        if len({item.recording_id for item in recordings}) != len(recordings):
            raise ValueError("recording_id values must be unique")
        ordered = sorted(
            recordings,
            key=lambda item: (item.started_at, item.ended_at, item.recording_id),
        )
        if len(ordered) < 2:
            return []

        by_id = {item.recording_id: item for item in ordered}
        parent = {item.recording_id: item.recording_id for item in ordered}
        component_members = {
            item.recording_id: {item.recording_id}
            for item in ordered
        }
        known_customers = {
            item.recording_id: (
                {item.customer_voiceprint_id} if item.customer_voiceprint_id else set()
            )
            for item in ordered
        }
        explicit_receptions = {
            item.recording_id: (
                {item.explicit_reception_id} if item.explicit_reception_id else set()
            )
            for item in ordered
        }
        explicit_sessions = {
            item.recording_id: ({item.explicit_session_id} if item.explicit_session_id else set())
            for item in ordered
        }

        def find(item_id: str) -> str:
            root = item_id
            while parent[root] != root:
                root = parent[root]
            while parent[item_id] != item_id:
                next_id = parent[item_id]
                parent[item_id] = root
                item_id = next_id
            return root

        def union(
            left_id: str,
            right_id: str,
            *,
            allow_customer_override: bool,
            allow_identity_override: bool,
        ) -> bool:
            left_root = find(left_id)
            right_root = find(right_id)
            if left_root == right_root:
                return True
            active_constraints = constraints or ManualReceptionConstraints()
            if any(
                frozenset((left_member, right_member))
                in active_constraints.force_split
                for left_member in component_members[left_root]
                for right_member in component_members[right_root]
            ):
                return False
            combined_customers = known_customers[left_root] | known_customers[right_root]
            combined_receptions = explicit_receptions[left_root] | explicit_receptions[right_root]
            combined_sessions = explicit_sessions[left_root] | explicit_sessions[right_root]
            if len(combined_customers) > 1 and not allow_customer_override:
                return False
            if (
                len(combined_receptions) > 1 or len(combined_sessions) > 1
            ) and not allow_identity_override:
                return False
            parent[right_root] = left_root
            component_members[left_root] |= component_members[right_root]
            known_customers[left_root] = combined_customers
            explicit_receptions[left_root] = combined_receptions
            explicit_sessions[left_root] = combined_sessions
            return True

        pair_proposals: list[ReceptionProposal] = []
        accepted_edges: list[ReceptionProposal] = []
        for index, left in enumerate(ordered):
            compared_neighbors = 0
            for right in ordered[index + 1 :]:
                if (
                    max_neighbors is not None
                    and right.started_at - left.ended_at > self.merge_window
                ):
                    break
                if max_neighbors is not None and compared_neighbors >= max_neighbors:
                    break
                compared_neighbors += 1
                proposal = self.evaluate_pair(left, right, constraints=constraints)
                pair_proposals.append(proposal)
                if proposal.decision != "merge":
                    continue
                codes = set(proposal.reason_codes)
                manual_merge = "manual_force_merge" in codes
                explicit_match = bool(
                    {"explicit_reception_match", "explicit_session_match"} & codes
                )
                if union(
                    left.recording_id,
                    right.recording_id,
                    allow_customer_override=explicit_match or manual_merge,
                    allow_identity_override=manual_merge,
                ):
                    accepted_edges.append(proposal)

        components: dict[str, list[RecordingCandidate]] = {}
        for recording in ordered:
            components.setdefault(find(recording.recording_id), []).append(recording)
        accepted_edges_by_component: dict[str, list[ReceptionProposal]] = {}
        for edge in accepted_edges:
            accepted_edges_by_component.setdefault(find(edge.recording_ids[0]), []).append(edge)

        output: list[ReceptionProposal] = []
        for component_id, members in components.items():
            if len(members) < 2:
                continue
            edges = accepted_edges_by_component[component_id]
            reasons = self._unique_reasons(reason for edge in edges for reason in edge.reasons)
            confidence = min(edge.confidence for edge in edges)
            ordered_ids = tuple(member.recording_id for member in members)
            output.append(
                ReceptionProposal(
                    recording_ids=ordered_ids,
                    decision="merge",
                    confidence=round(confidence, 4),
                    reasons=reasons,
                    manual_override=any(edge.manual_override for edge in edges),
                )
            )

        # Preserve unresolved candidates that are not already internal to a
        # successful component.  Reject edges are available via evaluate_pair
        # and intentionally omitted here to avoid O(n²) UI noise.
        for proposal in pair_proposals:
            if proposal.decision != "needs_review":
                continue
            if find(proposal.recording_ids[0]) == find(proposal.recording_ids[1]):
                continue
            output.append(proposal)

        return sorted(
            output,
            key=lambda proposal: min(
                by_id[recording_id].started_at for recording_id in proposal.recording_ids
            ),
        )

    def detect_recording_splits(
        self,
        turns: Sequence[ReceptionTurn],
        *,
        max_signals: int | None = None,
    ) -> list[ReceptionSplitSignal]:
        """Detect new receptions only when at least two independent signals agree."""
        if max_signals is not None and max_signals <= 0:
            raise ValueError("max_signals must be positive")
        ordered = sorted(
            turns,
            key=lambda turn: (turn.start_sec, turn.end_sec, turn.segment_id),
        )
        signals: list[ReceptionSplitSignal] = []
        for previous, current in pairwise(ordered):
            reasons: list[MergeFeatureReason] = []
            if any(phrase in current.transcript for phrase in self._REGREETING_PHRASES):
                reasons.append(
                    MergeFeatureReason(
                        "re_greeting",
                        0.34,
                        "agent opens a new greeting sequence",
                    )
                )
            pause = max(0.0, current.start_sec - previous.end_sec)
            if pause >= self.long_recording_pause_sec:
                reasons.append(
                    MergeFeatureReason(
                        "long_pause",
                        0.36,
                        f"pause={pause:.1f}s",
                    )
                )
            if (
                previous.customer_voiceprint_id
                and current.customer_voiceprint_id
                and previous.customer_voiceprint_id != current.customer_voiceprint_id
            ):
                reasons.append(
                    MergeFeatureReason(
                        "customer_change",
                        0.45,
                        (f"{previous.customer_voiceprint_id}->{current.customer_voiceprint_id}"),
                    )
                )

            independent_codes = {reason.code for reason in reasons}
            confidence = min(
                1.0,
                sum(reason.contribution for reason in reasons),
            )
            if len(independent_codes) >= 2 and confidence >= 0.70:
                signals.append(
                    ReceptionSplitSignal(
                        at_segment_id=current.segment_id,
                        at_sec=current.start_sec,
                        confidence=round(confidence, 4),
                        reasons=tuple(reasons),
                    )
                )
                if max_signals is not None and len(signals) >= max_signals:
                    break
        return signals

    def _explicit_identity_decision(
        self,
        first: RecordingCandidate,
        second: RecordingCandidate,
        *,
        attribute: Literal["explicit_reception_id", "explicit_session_id"],
        match_code: str,
        conflict_code: str,
        match_confidence: float,
    ) -> ReceptionProposal | None:
        left_value = getattr(first, attribute)
        right_value = getattr(second, attribute)
        if left_value and right_value and left_value == right_value:
            reasons = [
                MergeFeatureReason(
                    match_code,
                    1.0,
                    f"{attribute}={left_value}",
                    hard_constraint=True,
                )
            ]
            if self._known_customer_conflict(first, second):
                reasons.append(
                    MergeFeatureReason(
                        "voiceprint_conflict_overridden_by_explicit_identity",
                        -0.2,
                        "explicit identity outranks a potentially noisy voiceprint",
                    )
                )
            return self._proposal(
                first,
                second,
                "merge",
                match_confidence,
                *reasons,
            )
        if left_value and right_value and left_value != right_value:
            return self._proposal(
                first,
                second,
                "reject",
                0.99,
                MergeFeatureReason(
                    conflict_code,
                    -1.0,
                    f"{left_value}!={right_value}",
                    hard_constraint=True,
                ),
            )
        return None

    @staticmethod
    def _known_customer_conflict(
        first: RecordingCandidate,
        second: RecordingCandidate,
    ) -> bool:
        return bool(
            first.customer_voiceprint_id
            and second.customer_voiceprint_id
            and first.customer_voiceprint_id != second.customer_voiceprint_id
        )

    @staticmethod
    def _overlap_seconds(
        first: RecordingCandidate,
        second: RecordingCandidate,
    ) -> float:
        overlap = min(first.ended_at, second.ended_at) - max(
            first.started_at,
            second.started_at,
        )
        return max(0.0, overlap.total_seconds())

    @staticmethod
    def _identity_reasons(
        first: RecordingCandidate,
        second: RecordingCandidate,
    ) -> tuple[MergeFeatureReason, ...]:
        reasons: list[MergeFeatureReason] = []
        if first.agent_id and first.agent_id == second.agent_id:
            reasons.append(MergeFeatureReason("same_agent", 0.15, f"agent={first.agent_id}"))
        if (
            first.customer_voiceprint_id
            and first.customer_voiceprint_id == second.customer_voiceprint_id
        ):
            reasons.append(
                MergeFeatureReason(
                    "same_customer_voiceprint",
                    0.50,
                    f"voiceprint={first.customer_voiceprint_id}",
                )
            )
        return tuple(reasons)

    @staticmethod
    def _cosine_similarity(
        left: tuple[float, ...] | None,
        right: tuple[float, ...] | None,
    ) -> float | None:
        if left is None or right is None or len(left) != len(right) or not left:
            return None
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return None
        similarity = sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
        return max(-1.0, min(1.0, similarity))

    @staticmethod
    def _proposal(
        first: RecordingCandidate,
        second: RecordingCandidate,
        decision: ReceptionDecision,
        confidence: float,
        *reasons: MergeFeatureReason,
        manual_override: bool = False,
    ) -> ReceptionProposal:
        return ReceptionProposal(
            recording_ids=(first.recording_id, second.recording_id),
            decision=decision,
            confidence=round(max(0.0, min(1.0, confidence)), 4),
            reasons=tuple(reasons),
            manual_override=manual_override,
        )

    @staticmethod
    def _unique_reasons(
        reasons: Iterable[MergeFeatureReason],
    ) -> tuple[MergeFeatureReason, ...]:
        by_code: dict[str, MergeFeatureReason] = {}
        for reason in reasons:
            by_code.setdefault(reason.code, reason)
        return tuple(by_code.values())
