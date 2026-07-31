"""T7 — CommunitySummaryService (M9 architecture §8, Q2 ruling).

Generates natural-language summaries of Leiden communities, with the
strategy dictated by Q2:

    Level 0 (root)     → eager   (always generated on first Leiden run)
    Level 1-2          → lazy    (generated on first retrieval request)
    Level 3            → DROPPED (never generated)

The service is intentionally storage-agnostic: it accepts a
``SummarySink`` protocol for persistence so unit tests can substitute an
in-memory list.

Attribution: the level-hierarchy summarisation pattern follows
GraphRAG (Microsoft, 2024) — MIT-clean conceptual reference.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from audio_graphy.adapters.protocols import LLMAdapter, LLMResponse
from audio_graphy.core.leiden import LeidenRunResult
from audio_graphy.core.types import GraphEdge, GraphNode
from audio_graphy.services.llm_gateway import (
    CachePolicy,
    LLMProvenance,
    LLMRequest,
    canonical_sha256,
    execute_llm,
)

logger = logging.getLogger(__name__)

# Q2 — max hierarchy level that this service will summarise.
Q2_MAX_LEVEL: int = 2  # level 3 dropped
_COMMUNITY_PROMPT_VERSION = "community-summary-prompt-v1"
_COMMUNITY_SCHEMA_VERSION = "community-summary-schema-v1"
_COMMUNITY_PARSER_VERSION = "community-summary-parser-v1"
_COMMUNITY_POSTPROCESSOR_VERSION = "community-summary-postprocessor-v1"
_COMMUNITY_TTL_SECONDS = 90 * 24 * 60 * 60
_COMMUNITY_MAX_TOKENS = 256
_COMMUNITY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "string",
    "format": "title-summary-markers-v1",
    "required_markers": ["TITLE:", "SUMMARY:"],
    "title_max_length": 80,
    "summary_max_length": 4000,
}


# ============================================================
# Public types
# ============================================================


@dataclass(frozen=True, slots=True)
class CommunityMembership:
    """A flattened view of one community at one level.

    Attributes:
        level: Hierarchy depth (0..2).
        community_id: Leiden-assigned integer id.
        nodes: Member GraphNodes (snapshot at write time).
        edges: Internal edges (both endpoints inside the community).
        strategy: ``eager`` or ``lazy`` (per Q2).
    """

    level: int
    community_id: int
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    strategy: str


@dataclass(frozen=True, slots=True)
class CommunitySummaryRecord:
    """In-memory representation of one ``community_summaries`` row.

    Decoupled from the ORM so the service can be unit-tested without a DB.
    """

    leiden_job_id: int
    level: int
    community_id: int
    title: str
    summary: str
    member_count: int
    member_node_ids: list[str]
    generated_at: datetime
    strategy: str


class SummarySink(Protocol):
    """Persistence protocol for CommunitySummaryRecord writes."""

    def write(self, record: CommunitySummaryRecord, tenant_id: str) -> None: ...

    def fetch(
        self,
        *,
        leiden_job_id: int,
        level: int,
        community_id: int,
        tenant_id: str,
    ) -> CommunitySummaryRecord | None: ...


# ============================================================
# Default in-memory sink (used by tests + as a no-op default)
# ============================================================


class InMemorySummarySink:
    """List-backed sink — useful for tests and for eager cache priming."""

    def __init__(self) -> None:
        self.records: list[CommunitySummaryRecord] = []

    def write(self, record: CommunitySummaryRecord, tenant_id: str) -> None:
        self.records.append(record)

    def fetch(
        self,
        *,
        leiden_job_id: int,
        level: int,
        community_id: int,
        tenant_id: str,
    ) -> CommunitySummaryRecord | None:
        for r in self.records:
            if (
                r.leiden_job_id == leiden_job_id
                and r.level == level
                and r.community_id == community_id
            ):
                return r
        return None


# ============================================================
# Service
# ============================================================


class CommunitySummaryService:
    """Generate + cache community summaries per Q2 ruling.

    Args:
        llm: Adapter used to generate summaries (eager + lazy paths).
        sink: Persistence backend (DB ORM adapter or in-memory).
        prompt_template: Template with ``{level}`` / ``{nodes}`` /
            ``{edges}`` placeholders.
        tenant_id: Tenant scope.
        leiden_job_id: Job id used as the FK parent in community_summaries.
    """

    def __init__(
        self,
        *,
        llm: LLMAdapter,
        sink: SummarySink,
        prompt_template: str,
        tenant_id: str,
        leiden_job_id: int,
    ) -> None:
        self._llm = llm
        self._sink = sink
        self._prompt_template = prompt_template
        self._tenant_id = tenant_id
        self._leiden_job_id = leiden_job_id

    # ------------------------------------------------------------
    # Membership extraction
    # ------------------------------------------------------------

    def build_memberships(
        self,
        *,
        leiden_result: LeidenRunResult,
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
    ) -> list[CommunityMembership]:
        """Project Leiden output into per-community memberships.

        Per Q2, only levels 0..Q2_MAX_LEVEL are materialised. Level 3 and
        above are dropped silently.
        """
        node_by_id: dict[str, GraphNode] = {n.entity_id: n for n in nodes}
        levels = _build_level_mappings(
            leiden_result.node_to_community,
            max_levels=Q2_MAX_LEVEL,
        )
        out: list[CommunityMembership] = []
        for level_idx, mapping in enumerate(levels):
            if level_idx > Q2_MAX_LEVEL:
                break
            strategy = "eager" if level_idx == 0 else "lazy"
            # Group nodes by their community id at this level.
            by_comm: dict[int, list[GraphNode]] = {}
            for nid, cid in mapping.items():
                if nid in node_by_id:
                    by_comm.setdefault(cid, []).append(node_by_id[nid])
            for cid, members in by_comm.items():
                internal_edges = [
                    e
                    for e in edges
                    if e.source in {m.entity_id for m in members}
                    and e.target in {m.entity_id for m in members}
                ]
                out.append(
                    CommunityMembership(
                        level=level_idx,
                        community_id=cid,
                        nodes=members,
                        edges=internal_edges,
                        strategy=strategy,
                    )
                )
        return out

    # ------------------------------------------------------------
    # Eager generation (Q2: level 0 + leaves)
    # ------------------------------------------------------------

    async def generate_eager(
        self,
        memberships: Iterable[CommunityMembership],
    ) -> list[CommunitySummaryRecord]:
        """Generate summaries for level 0 + every leaf community.

        Per Q2: levels 1-2 are lazy (skipped here); level 3 was dropped
        in ``build_memberships`` and never reaches this method.
        """
        records: list[CommunitySummaryRecord] = []
        for m in memberships:
            is_leaf = self._is_leaf(m)
            if m.level != 0 and not is_leaf:
                continue
            cached = self._sink.fetch(
                leiden_job_id=self._leiden_job_id,
                level=m.level,
                community_id=m.community_id,
                tenant_id=self._tenant_id,
            )
            if cached is not None:
                records.append(cached)
                continue
            rec = await self._generate_one(m)
            records.append(rec)
            self._sink.write(rec, self._tenant_id)
        return records

    # ------------------------------------------------------------
    # Lazy generation (Q2: levels 1-2)
    # ------------------------------------------------------------

    async def get_or_generate(
        self,
        *,
        level: int,
        community_id: int,
        memberships: Iterable[CommunityMembership],
    ) -> CommunitySummaryRecord:
        """Return cached summary; generate + cache if missing.

        Used by retrieval: when the GraphRAG map-reduce needs a level-1
        or level-2 summary that doesn't yet exist, this method generates
        it on demand and persists for future lookups.
        """
        if level < 0 or level > Q2_MAX_LEVEL:
            raise ValueError(f"level {level} out of range (Q2 cap={Q2_MAX_LEVEL})")

        cached = self._sink.fetch(
            leiden_job_id=self._leiden_job_id,
            level=level,
            community_id=community_id,
            tenant_id=self._tenant_id,
        )
        if cached is not None:
            return cached

        # Find the matching membership and generate.
        for m in memberships:
            if m.level == level and m.community_id == community_id:
                rec = await self._generate_one(m)
                self._sink.write(rec, self._tenant_id)
                return rec
        raise KeyError(f"no membership for level={level} community_id={community_id}")

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _is_leaf(self, m: CommunityMembership) -> bool:
        """A leaf community is at the deepest computed level (Q2_MAX_LEVEL)."""
        return m.level == Q2_MAX_LEVEL

    async def _generate_one(self, m: CommunityMembership) -> CommunitySummaryRecord:
        """Render prompt → call LLM → parse → return record."""
        ordered_nodes = sorted(m.nodes, key=lambda node: node.entity_id)
        ordered_edges = sorted(
            m.edges,
            key=lambda edge: (edge.source, edge.relation, edge.target),
        )
        nodes_str = "\n".join(
            f"- {node.entity_id} ({node.type}): {node.description}" for node in ordered_nodes
        )
        edges_str = "\n".join(
            f"- {edge.source} --{edge.relation}--> {edge.target}" for edge in ordered_edges
        )
        user_content = (
            "COMMUNITY INPUT\n"
            f"LEVEL: {m.level}\n"
            f"COMMUNITY_ID: {m.community_id}\n"
            f"NODES:\n{nodes_str or '(none)'}\n"
            f"EDGES:\n{edges_str or '(none)'}"
        )
        content_snapshot = {
            "level": m.level,
            "community_id": m.community_id,
            "nodes": [
                {
                    "entity_id": node.entity_id,
                    "name": node.name,
                    "type": node.type,
                    "description": node.description,
                    "source_ids": sorted(node.source_ids),
                    "recording_ids": sorted(node.recording_ids),
                    "degree": node.degree,
                    "expired_at": node.expired_at,
                }
                for node in ordered_nodes
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "relation": edge.relation,
                    "weight": edge.weight,
                    "confidence": edge.confidence,
                    "confidence_score": edge.confidence_score,
                    "source_ids": sorted(edge.source_ids),
                    "valid_at": edge.valid_at,
                    "invalid_at": edge.invalid_at,
                    "created_at": edge.created_at,
                    "expired_at": edge.expired_at,
                    "superseded_by": edge.superseded_by,
                }
                for edge in ordered_edges
            ],
        }
        system_prompt = self._prompt_template.strip()
        prompt_sha256 = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        recording_refs = tuple(
            LLMProvenance("recording", recording_id)
            for recording_id in sorted(
                {str(recording_id) for node in ordered_nodes for recording_id in node.recording_ids}
            )
        )
        request = LLMRequest(
            tenant_id=self._tenant_id,
            purpose="community_summary",
            model_tier="weak",
            provider=str(getattr(self._llm, "provider", "openai-compatible")),
            model_epoch=str(getattr(self._llm, "model_epoch", self._llm.model)),
            messages=(
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ),
            prompt_version=f"{_COMMUNITY_PROMPT_VERSION}:{prompt_sha256}",
            schema_version=_COMMUNITY_SCHEMA_VERSION,
            parser_version=_COMMUNITY_PARSER_VERSION,
            postprocessor_version=_COMMUNITY_POSTPROCESSOR_VERSION,
            temperature=0.0,
            top_p=1.0,
            max_tokens=_COMMUNITY_MAX_TOKENS,
            response_schema=_COMMUNITY_RESPONSE_SCHEMA,
            business_snapshot={
                "content_sha256": canonical_sha256(content_snapshot),
                "member_count": len(ordered_nodes),
            },
            permission_scope={
                "tenant_id": self._tenant_id,
                "leiden_job_id": self._leiden_job_id,
                "community_id": m.community_id,
                "level": m.level,
            },
            provenance=(
                LLMProvenance(
                    "community",
                    f"{self._leiden_job_id}:{m.level}:{m.community_id}",
                ),
                LLMProvenance("leiden_job", str(self._leiden_job_id)),
                *recording_refs,
            ),
            cache_policy=CachePolicy.EXACT,
            ttl_seconds=_COMMUNITY_TTL_SECONDS,
            response_validator=_valid_summary_response,
        )
        response = await execute_llm(self._llm, request)
        parsed = _parse_structured_llm_output(response.text)
        if parsed is None:
            raise ValueError("community summary LLM output failed structured validation")
        title, summary = parsed
        return CommunitySummaryRecord(
            leiden_job_id=self._leiden_job_id,
            level=m.level,
            community_id=m.community_id,
            title=title,
            summary=summary,
            member_count=len(ordered_nodes),
            member_node_ids=[node.entity_id for node in ordered_nodes],
            generated_at=datetime.now(UTC),
            strategy=m.strategy,
        )


# ============================================================
# Helpers
# ============================================================


def _build_level_mappings(
    base: dict[str, int],
    *,
    max_levels: int,
) -> list[dict[str, int]]:
    """Reuse ``compute_hierarchy_levels`` but tolerate empty input."""
    if not base:
        return [{}]
    from audio_graphy.core.leiden import compute_hierarchy_levels

    return compute_hierarchy_levels(base, max_levels=max_levels)


def _parse_llm_output(raw: str) -> tuple[str, str]:
    """Parse ``TITLE: ...\\nSUMMARY: ...`` LLM output (best-effort)."""
    structured = _parse_structured_llm_output(raw)
    if structured is not None:
        return structured
    title = "Untitled community"
    summary = raw.strip()
    lines = raw.strip().splitlines()
    title_line_idx = next(
        (i for i, ln in enumerate(lines) if ln.upper().startswith("TITLE:")),
        None,
    )
    summary_line_idx = next(
        (i for i, ln in enumerate(lines) if ln.upper().startswith("SUMMARY:")),
        None,
    )
    if title_line_idx is not None:
        title = lines[title_line_idx][len("TITLE:") :].strip()[:80]
    if summary_line_idx is not None:
        # First line's content after "SUMMARY:" + any continuation lines.
        first_part = lines[summary_line_idx][len("SUMMARY:") :].strip()
        rest = "\n".join(lines[summary_line_idx + 1 :]).strip()
        summary = (f"{first_part}\n{rest}" if first_part else rest) if rest else first_part
    if not title:
        title = "Untitled community"
    if not summary:
        summary = "(empty summary)"
    return title, summary


def _parse_structured_llm_output(raw: str) -> tuple[str, str] | None:
    """Strictly validate the cacheable TITLE/SUMMARY wire format."""

    if not isinstance(raw, str) or not raw.strip() or len(raw) > 16_384:
        return None
    lines = raw.strip().splitlines()
    if len(lines) < 2:
        return None
    if not lines[0].upper().startswith("TITLE:"):
        return None
    if not lines[1].upper().startswith("SUMMARY:"):
        return None
    title = lines[0][len("TITLE:") :].strip()
    first_summary_part = lines[1][len("SUMMARY:") :].strip()
    continuation = "\n".join(lines[2:]).strip()
    summary = (
        f"{first_summary_part}\n{continuation}"
        if first_summary_part and continuation
        else first_summary_part or continuation
    )
    if not 1 <= len(title) <= 80 or not 1 <= len(summary) <= 4000:
        return None
    return title, summary


def _valid_summary_response(response: LLMResponse) -> bool:
    """Gateway validator: malformed/incomplete results must never be cached."""

    return _parse_structured_llm_output(response.text) is not None


# ============================================================
# Factory
# ============================================================


PromptLoader = Callable[[], Awaitable[str]]


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


async def load_default_prompt(
    prompt_root: Any | None = None,
) -> str:
    """Load the bundled prompt template (community_summary_v1.txt).

    Args:
        prompt_root: Optional Path-like with a ``community_summary_v1.txt``
            file. Defaults to the bundled prompts directory.
    """
    root = Path(prompt_root) if prompt_root is not None else _PROMPTS_DIR
    path = root / "community_summary_v1.txt"
    return path.read_text(encoding="utf-8")
