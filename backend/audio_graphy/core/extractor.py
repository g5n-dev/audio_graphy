"""Entity extractor — GraphRAG delimiter protocol parsing + Gleaning + Chinese normalisation.

Pipeline per chunk:
    1. Build extraction prompt (with GraphRAG delimiters + entity types + few-shot)
    2. Execute a tenant-scoped strong-model request through LLMGateway
    3. Parse LLM output: split by delimiters → ExtractedEntity[] + ExtractedRelation[]
    4. Gleaning: ask LLM if anything was missed → supplement extraction
    5. Chinese entity normalisation: alias table + edit-distance clustering

Parser strategy (architecture §1.4):
    - Primary: split by TUPLE_DELIMITER / RECORD_DELIMITER / COMPLETION_DELIMITER
    - Fallback: CSV-style quoted fields (for mock LLM compatibility)
    - Lenient regex: extract partial matches, mark parse_success=False

LLM cache:
    - Centralized LLMGateway owns hot/persistent caching and transient retries.
    - FileIndex is never used for LLM result caching.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.protocols import EdgeConfidence, LLMResponse
from audio_graphy.core.types import (
    COMPLETION_DELIMITER,
    DEFAULT_ENTITY_TYPES,
    RECORD_DELIMITER,
    TUPLE_DELIMITER,
)
from audio_graphy.services.llm_gateway import (
    CachePolicy,
    LLMProvenance,
    LLMRequest,
    execute_llm,
)

if TYPE_CHECKING:
    from audio_graphy.core.entity_merger import EntityMerger
    from audio_graphy.storage.file_index import FileIndex

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT_VERSION = "entity-relation-extract-v1"
_GLEANING_PROMPT_VERSION = "entity-relation-gleaning-v1"
_EXTRACTION_PARSER_VERSION = "graphrag-delimiter-parser-v1"
_EXTRACTION_POSTPROCESSOR_VERSION = "chinese-entity-normalisation-v1"
_EXTRACTION_TTL_SECONDS = 90 * 24 * 60 * 60
_EXPLICIT_EMPTY_RESPONSES = frozenset(
    {
        COMPLETION_DELIMITER.casefold(),
        "无",
        "无新增实体或关系",
        "none",
        "no additional entities or relationships",
    }
)

# Default alias table for Chinese entity normalisation (DESIGN.md §5.2)
_DEFAULT_ALIASES: dict[str, str] = {
    "CS75PLUS": "CS75 Plus",
    "CS75plus": "CS75 Plus",
    "cs75 plus": "CS75 Plus",
    "长安CS75": "CS75 Plus",
    "UNI-V": "UNI-V",
    "UNIV": "UNI-V",
    "哈弗H6": "哈弗H6",
    "哈弗 H6": "哈弗H6",
}


# ============================================================
# Data classes
# ============================================================


@dataclass(frozen=True, slots=True)
class ExtractedEntity:
    """A single entity extracted from a chunk.

    Attributes:
        name: Entity name (pre-normalisation).
        type: Domain type (客户/坐席/车型/价格方案/金融政策/优惠权益/竞品/预约事件).
        description: Entity description from LLM.
        chunk_id: Source chunk database ID.
        recording_id: Source recording ID.
    """

    name: str
    type: str
    description: str
    chunk_id: int
    recording_id: int


@dataclass(frozen=True, slots=True)
class ExtractedRelation:
    """A single relation extracted from a chunk.

    Attributes:
        source_name: Source entity name (pre-normalisation).
        target_name: Target entity name (pre-normalisation).
        relation: Relation description (e.g. "推荐", "询问").
        description: Relation detail from LLM.
        weight: Edge weight (default 1.0).
        confidence: EXTRACTED (first round) or INFERRED (Gleaning).
        chunk_id: Source chunk database ID.
        recording_id: Source recording ID.
    """

    source_name: str
    target_name: str
    relation: str
    description: str
    weight: float
    confidence: EdgeConfidence
    chunk_id: int
    recording_id: int


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Complete extraction result for a single chunk.

    Attributes:
        chunk_id: Source chunk ID.
        recording_id: Source recording ID.
        entities: Extracted entities.
        relations: Extracted relations.
        parse_success: Whether the LLM output was successfully parsed.
        gleaning_rounds: Actual number of Gleaning rounds executed.
    """

    chunk_id: int
    recording_id: int
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]
    parse_success: bool
    gleaning_rounds: int


# ============================================================
# Entity extractor
# ============================================================


class EntityExtractor:
    """GraphRAG-style entity extraction with Gleaning and Chinese normalisation.

    Args:
        bundle: AdapterBundle (uses strong_llm).
        prompt_template: Prompt template with {tuple_delimiter} /
            {record_delimiter} / {completion_delimiter} / {entity_types} /
            {input_text} placeholders.
        gleaning_rounds: Number of Gleaning supplement rounds (default 1).
        adaptive_gleaning: Opt-in quality-gated mode that continues up to
            three rounds only while each preceding round finds new facts.
        entity_types: Domain entity types tuple.
        max_gleaning_retry: Deprecated compatibility argument. Retry policy is
            owned by LLMGateway and only covers transient failures.
        file_index: Deprecated compatibility argument. LLM results are never
            read from or written to FileIndex.
        aliases: Entity name alias table for Chinese normalisation.
            Used as Layer-3 fallback when ``entity_merger`` is None.
            Ignored when ``entity_merger`` is provided.
        entity_merger: Optional ``EntityMerger`` instance for 3-layer
            normalisation (DB alias → rapidfuzz → new canonical). When
            provided, replaces the legacy ``aliases`` dict path. Caller
            is responsible for the merger's lifecycle (typically
            constructed per-tenant in the ingestion service).
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        *,
        prompt_template: str,
        gleaning_rounds: int = 1,
        adaptive_gleaning: bool = False,
        entity_types: tuple[str, ...] = DEFAULT_ENTITY_TYPES,
        max_gleaning_retry: int = 2,
        file_index: FileIndex | None = None,
        aliases: dict[str, str] | None = None,
        entity_merger: EntityMerger | None = None,
    ) -> None:
        self._bundle = bundle
        self._prompt_template = prompt_template
        self._gleaning_rounds = gleaning_rounds
        self._adaptive_gleaning = adaptive_gleaning
        self._entity_types = entity_types
        if max_gleaning_retry != 2:
            logger.debug(
                "Ignoring max_gleaning_retry=%d; LLMGateway owns retries", max_gleaning_retry
            )
        if file_index is not None:
            logger.debug("EntityExtractor FileIndex LLM cache is disabled; using LLMGateway")
        self._aliases = aliases if aliases is not None else dict(_DEFAULT_ALIASES)
        self._entity_merger = entity_merger

    async def extract_from_chunk(
        self,
        chunk_id: int,
        chunk_text: str,
        recording_id: int,
        *,
        tenant_id: str = "default",
    ) -> ExtractionResult:
        """Extract entities and relations from a single chunk.

        Args:
            chunk_id: Chunk database ID.
            chunk_text: Chunk text content.
            recording_id: Recording ID for provenance.
            tenant_id: Tenant scope for cache and persistence isolation.

        Returns:
            ExtractionResult with entities, relations, and metadata.
        """
        # Handle empty text
        if not chunk_text or not chunk_text.strip():
            return ExtractionResult(
                chunk_id=chunk_id,
                recording_id=recording_id,
                entities=[],
                relations=[],
                parse_success=True,
                gleaning_rounds=0,
            )

        # Step 1: First-round extraction
        response = await self._execute_extraction_request(
            system_prompt=self._build_prompt(""),
            user_content=chunk_text,
            purpose="entity_relation_extract",
            prompt_version=_EXTRACTION_PROMPT_VERSION,
            tenant_id=tenant_id,
            chunk_id=chunk_id,
            recording_id=recording_id,
            chunk_text=chunk_text,
            phase_snapshot={},
        )
        entities, relations, parse_success = self._parse_llm_output(
            response.text, chunk_id, recording_id
        )

        # Step 2: Gleaning rounds
        actual_gleaning_rounds = 0
        # The opt-in adaptive mode permits additional rounds only while the
        # preceding round keeps finding new facts. This preserves the
        # established one-round path by default and avoids paying a fixed
        # three-round cost for already-complete chunks.
        gleaning_limit = (
            max(self._gleaning_rounds, 3) if self._adaptive_gleaning else self._gleaning_rounds
        )
        for _round_idx in range(gleaning_limit):
            new_entities, new_relations = await self._glean(
                entities,
                relations,
                chunk_text,
                chunk_id,
                recording_id,
                tenant_id=tenant_id,
            )
            actual_gleaning_rounds += 1
            existing_entities = {(item.name, item.type) for item in entities}
            existing_relations = {
                (item.source_name, item.relation, item.target_name) for item in relations
            }
            new_entities = [
                item for item in new_entities if (item.name, item.type) not in existing_entities
            ]
            new_relations = [
                item
                for item in new_relations
                if (item.source_name, item.relation, item.target_name) not in existing_relations
            ]

            if not new_entities and not new_relations:
                # No new findings — early termination
                break

            entities.extend(new_entities)
            relations.extend(new_relations)

        # Step 3: Chinese entity normalisation
        entities = await self._normalize_entities(entities)
        relations = self._normalize_relations(relations, entities)

        return ExtractionResult(
            chunk_id=chunk_id,
            recording_id=recording_id,
            entities=entities,
            relations=relations,
            parse_success=parse_success,
            gleaning_rounds=actual_gleaning_rounds,
        )

    async def extract_from_chunks(
        self,
        chunks: Sequence[tuple[int, str, int]],
        *,
        concurrency: int = 4,
        tenant_id: str = "default",
    ) -> list[ExtractionResult]:
        """Extract from multiple chunks concurrently.

        Args:
            chunks: Sequence of (chunk_id, text, recording_id) tuples.
            concurrency: Max concurrent LLM calls.
            tenant_id: Tenant scope shared by all chunks.

        Returns:
            List of ExtractionResult (one per chunk, in input order).
        """
        semaphore = asyncio.Semaphore(concurrency)

        async def _extract_with_limit(chunk_id: int, text: str, rec_id: int) -> ExtractionResult:
            async with semaphore:
                return await self.extract_from_chunk(
                    chunk_id,
                    text,
                    rec_id,
                    tenant_id=tenant_id,
                )

        tasks = [_extract_with_limit(chunk_id, text, rec_id) for chunk_id, text, rec_id in chunks]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert exceptions to empty results (don't block the pipeline)
        final: list[ExtractionResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                chunk_id, _, rec_id = chunks[i]
                logger.warning("Extraction failed for chunk %d: %s", chunk_id, result)
                final.append(
                    ExtractionResult(
                        chunk_id=chunk_id,
                        recording_id=rec_id,
                        entities=[],
                        relations=[],
                        parse_success=False,
                        gleaning_rounds=0,
                    )
                )
            else:
                final.append(result)  # type: ignore[arg-type]

        return final

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_prompt(self, chunk_text: str) -> str:
        """Build the extraction prompt from the template.

        Args:
            chunk_text: Chunk text to extract from.

        Returns:
            Formatted prompt string.
        """
        return self._prompt_template.format(
            tuple_delimiter=TUPLE_DELIMITER,
            record_delimiter=RECORD_DELIMITER,
            completion_delimiter=COMPLETION_DELIMITER,
            entity_types=", ".join(self._entity_types),
            input_text=chunk_text,
        )

    def _build_gleaning_prompt(
        self,
        entities: list[ExtractedEntity],
        relations: list[ExtractedRelation],
        chunk_text: str,
    ) -> str:
        """Build the Gleaning supplement prompt.

        Args:
            entities: Already-extracted entities.
            relations: Already-extracted relations.
            chunk_text: Original chunk text.

        Returns:
            Gleaning prompt string.
        """
        entity_summary = ", ".join(f"{e.name}({e.type})" for e in entities)
        relation_summary = ", ".join(
            f"{r.source_name}-{r.relation}->{r.target_name}" for r in relations
        )
        return (
            "请检查以下已抽取的实体和关系列表，判断是否遗漏了对话中提到的实体或关系。"
            "如果发现遗漏，请补充抽取。\n\n"
            f"已抽取实体: {entity_summary}\n"
            f"已抽取关系: {relation_summary}\n\n"
            f"原始文本:\n{chunk_text}\n\n"
            "请只输出新增的实体和关系。格式:\n"
            f'("实体"{TUPLE_DELIMITER}名称{TUPLE_DELIMITER}类型{TUPLE_DELIMITER}描述)'
            f"{RECORD_DELIMITER}"
            f'("关系"{TUPLE_DELIMITER}源实体{TUPLE_DELIMITER}关系{TUPLE_DELIMITER}'
            f"目标实体{TUPLE_DELIMITER}描述)"
            f"{COMPLETION_DELIMITER}"
        )

    # ------------------------------------------------------------------
    # LLM output parsing
    # ------------------------------------------------------------------

    def _parse_llm_output(
        self,
        text: str,
        chunk_id: int,
        recording_id: int,
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation], bool]:
        """Parse LLM output in GraphRAG delimiter or CSV format.

        Args:
            text: Raw LLM response text.
            chunk_id: Current chunk ID for provenance.
            recording_id: Current recording ID for provenance.

        Returns:
            Tuple of (entities, relations, parse_success).
        """
        # Step 1: Extract content before COMPLETION_DELIMITER
        content = text.split(COMPLETION_DELIMITER)[0] if COMPLETION_DELIMITER in text else text

        # Step 2: Split into records
        if RECORD_DELIMITER in content:
            records = content.split(RECORD_DELIMITER)
        else:
            # Fallback: split by newlines (CSV-style mock LLM output)
            records = content.strip().split("\n")

        entities: list[ExtractedEntity] = []
        relations: list[ExtractedRelation] = []
        any_parsed = False

        for record in records:
            record = record.strip()
            if not record:
                continue

            fields = self._extract_fields(record)
            if not fields:
                continue

            record_type = fields[0].strip()

            if record_type in ("实体", "entity"):
                if len(fields) >= 4:
                    entities.append(
                        ExtractedEntity(
                            name=fields[1],
                            type=fields[2],
                            description=fields[3],
                            chunk_id=chunk_id,
                            recording_id=recording_id,
                        )
                    )
                    any_parsed = True
            elif record_type in ("关系", "relation") and len(fields) >= 5:
                relations.append(
                    ExtractedRelation(
                        source_name=fields[1],
                        target_name=fields[3],
                        relation=fields[2],
                        description=fields[4],
                        weight=1.0,
                        confidence="EXTRACTED",
                        chunk_id=chunk_id,
                        recording_id=recording_id,
                    )
                )
                any_parsed = True

        return entities, relations, any_parsed

    @staticmethod
    def _extract_fields(record: str) -> list[str]:
        """Extract fields from a record string.

        Handles both GraphRAG delimiter (<|>) and CSV (,) formats.
        Strips quotes and whitespace from each field.

        Args:
            record: A single record string (e.g. ``("实体"<|>CS75 Plus<|>车型<|>描述)``).

        Returns:
            List of field strings.
        """
        record = record.strip()
        # Remove outer parentheses
        if record.startswith("(") and record.endswith(")"):
            record = record[1:-1]

        # Try GraphRAG delimiter first
        if TUPLE_DELIMITER in record:
            return [EntityExtractor._clean_field(f) for f in record.split(TUPLE_DELIMITER)]

        # Try CSV format — extract quoted or unquoted fields
        pattern = r'"([^"]*)"|\'([^\']*)\'|([^,]+)'
        matches = re.findall(pattern, record)
        fields: list[str] = []
        for match in matches:
            field = next((g for g in match if g is not None and g.strip()), "")
            fields.append(EntityExtractor._clean_field(field))

        return fields if fields else []

    @staticmethod
    def _clean_field(field: str) -> str:
        """Clean a field: strip whitespace, quotes, and control characters."""
        field = field.strip()
        # Remove surrounding quotes
        if len(field) >= 2 and field[0] in ('"', "'") and field[-1] == field[0]:
            field = field[1:-1]
        return field.strip()

    # ------------------------------------------------------------------
    # Gleaning
    # ------------------------------------------------------------------

    async def _glean(
        self,
        entities: list[ExtractedEntity],
        relations: list[ExtractedRelation],
        chunk_text: str,
        chunk_id: int,
        recording_id: int,
        *,
        tenant_id: str = "default",
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation]]:
        """Perform one Gleaning supplement round.

        Asks the LLM if any entities/relations were missed. New relations
        from Gleaning get confidence=INFERRED.

        Args:
            entities: Already-extracted entities.
            relations: Already-extracted relations.
            chunk_text: Original chunk text.
            chunk_id: Current chunk ID.
            recording_id: Current recording ID.
            tenant_id: Tenant scope for cache isolation.

        Returns:
            Tuple of (new_entities, new_relations) from Gleaning.
        """
        entity_summary = ", ".join(f"{entity.name}({entity.type})" for entity in entities)
        relation_summary = ", ".join(
            f"{relation.source_name}-{relation.relation}->{relation.target_name}"
            for relation in relations
        )
        dynamic_input = (
            f"已抽取实体: {entity_summary}\n"
            f"已抽取关系: {relation_summary}\n\n"
            f"原始文本:\n{chunk_text}"
        )
        try:
            response = await self._execute_extraction_request(
                system_prompt=self._build_gleaning_prompt([], [], ""),
                user_content=dynamic_input,
                purpose="entity_relation_gleaning",
                prompt_version=_GLEANING_PROMPT_VERSION,
                tenant_id=tenant_id,
                chunk_id=chunk_id,
                recording_id=recording_id,
                chunk_text=chunk_text,
                phase_snapshot={
                    "existing_entities": [
                        {"name": entity.name, "type": entity.type} for entity in entities
                    ],
                    "existing_relations": [
                        {
                            "source": relation.source_name,
                            "relation": relation.relation,
                            "target": relation.target_name,
                        }
                        for relation in relations
                    ],
                },
            )
        except Exception as exc:
            # Gateway owns bounded retry of transient failures. Gleaning stays
            # optional and fails open after this single logical request.
            logger.warning("Gleaning LLM call failed: %s", exc)
            return [], []

        new_entities, new_relations, _ = self._parse_llm_output(
            response.text, chunk_id, recording_id
        )

        # Mark Gleaning relations as INFERRED
        inferred_relations = [
            ExtractedRelation(
                source_name=r.source_name,
                target_name=r.target_name,
                relation=r.relation,
                description=r.description,
                weight=r.weight,
                confidence="INFERRED",
                chunk_id=r.chunk_id,
                recording_id=r.recording_id,
            )
            for r in new_relations
        ]

        return new_entities, inferred_relations

    # ------------------------------------------------------------------
    # Chinese entity normalisation
    # ------------------------------------------------------------------

    async def _normalize_entities(self, entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
        """Normalise entity names using alias table or 3-layer ``EntityMerger``.

        Args:
            entities: Raw extracted entities.

        Returns:
            Entities with normalised names.
        """
        if not entities:
            return entities

        # M6: prefer 3-layer EntityMerger (DB alias → rapidfuzz → new canonical).
        if self._entity_merger is not None:
            pairs = [(e.name, e.type) for e in entities]
            merged_pairs = await self._entity_merger.merge(pairs)
            return [
                ExtractedEntity(
                    name=merged_pairs[i][0],
                    type=ent.type,
                    description=ent.description,
                    chunk_id=ent.chunk_id,
                    recording_id=ent.recording_id,
                )
                for i, ent in enumerate(entities)
            ]

        # Legacy fallback (M5): hard-coded _DEFAULT_ALIASES dict.
        normalised: list[ExtractedEntity] = []
        for ent in entities:
            normalised_name = self._aliases.get(ent.name, ent.name)
            normalised.append(
                ExtractedEntity(
                    name=normalised_name,
                    type=ent.type,
                    description=ent.description,
                    chunk_id=ent.chunk_id,
                    recording_id=ent.recording_id,
                )
            )
        return normalised

    def _normalize_relations(
        self,
        relations: list[ExtractedRelation],
        entities: list[ExtractedEntity],
    ) -> list[ExtractedRelation]:
        """Normalise relation entity names to match normalised entity names.

        Args:
            relations: Raw extracted relations.
            entities: Normalised entities (for name reference).

        Returns:
            Relations with normalised source/target names.
        """
        normalised: list[ExtractedRelation] = []
        for rel in relations:
            source = self._aliases.get(rel.source_name, rel.source_name)
            target = self._aliases.get(rel.target_name, rel.target_name)
            normalised.append(
                ExtractedRelation(
                    source_name=source,
                    target_name=target,
                    relation=rel.relation,
                    description=rel.description,
                    weight=rel.weight,
                    confidence=rel.confidence,
                    chunk_id=rel.chunk_id,
                    recording_id=rel.recording_id,
                )
            )
        return normalised

    # ------------------------------------------------------------------
    # Centralized LLM execution
    # ------------------------------------------------------------------

    def _valid_extraction_response(
        self,
        response: LLMResponse,
        *,
        chunk_id: int,
        recording_id: int,
    ) -> bool:
        """Accept parsed GraphRAG records or an explicit legal-empty sentinel."""

        _entities, _relations, parsed = self._parse_llm_output(
            response.text,
            chunk_id,
            recording_id,
        )
        if parsed:
            return True
        return response.text.strip().casefold() in _EXPLICIT_EMPTY_RESPONSES

    async def _execute_extraction_request(
        self,
        *,
        system_prompt: str,
        user_content: str,
        purpose: str,
        prompt_version: str,
        tenant_id: str,
        chunk_id: int,
        recording_id: int,
        chunk_text: str,
        phase_snapshot: dict[str, object],
    ) -> LLMResponse:
        """Execute one extraction/gleaning request through LLMGateway."""

        adapter = self._bundle.strong_llm
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        prompt_sha256 = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        schema_sha256 = hashlib.sha256(
            "\0".join(
                (
                    *self._entity_types,
                    TUPLE_DELIMITER,
                    RECORD_DELIMITER,
                    COMPLETION_DELIMITER,
                )
            ).encode("utf-8")
        ).hexdigest()
        request = LLMRequest(
            tenant_id=tenant_id,
            purpose=purpose,
            model_tier="strong",
            provider=str(getattr(adapter, "provider", "openai-compatible")),
            model_epoch=str(getattr(adapter, "model_epoch", adapter.model)),
            messages=messages,
            prompt_version=f"{prompt_version}:{prompt_sha256}",
            schema_version=f"graphrag-entity-schema-v1:{schema_sha256}",
            parser_version=_EXTRACTION_PARSER_VERSION,
            postprocessor_version=_EXTRACTION_POSTPROCESSOR_VERSION,
            temperature=0.0,
            top_p=1.0,
            response_schema={
                "type": "string",
                "format": "graphrag-delimited-entity-relation-records",
            },
            business_snapshot={
                "recording_id": recording_id,
                "chunk_id": chunk_id,
                "chunk_text_sha256": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                "phase": purpose,
                **phase_snapshot,
            },
            permission_scope={
                "tenant_id": tenant_id,
                "recording_id": recording_id,
            },
            provenance=(
                LLMProvenance("recording", str(recording_id)),
                LLMProvenance("chunk", str(chunk_id)),
            ),
            cache_policy=CachePolicy.EXACT,
            ttl_seconds=_EXTRACTION_TTL_SECONDS,
            response_validator=lambda response: self._valid_extraction_response(
                response,
                chunk_id=chunk_id,
                recording_id=recording_id,
            ),
        )
        return await execute_llm(adapter, request)


# ============================================================
# M7 — speaker entity injection helpers
# ============================================================


def build_speaker_entities_from_segments(
    segment_speakers: Sequence[tuple[int, str | None, int]],
    *,
    ambiguity_map: dict[str, str | None] | None = None,
) -> list[tuple[str, str, str]]:
    """Build (name, type, description) triples for each distinct speaker.

    Used by the indexing service after chunker produces ``SegmentRecord.speaker``
    values. The resulting triples are merged into ``ExtractionResult.entities``
    so the graph builder creates SPEAKER nodes alongside LLM-extracted entities.

    Args:
        segment_speakers: ``(segment_idx, speaker_id, recording_id)`` tuples.
            ``speaker_id`` may be ``None`` (no diarization) — those entries
            produce no entity.
        ambiguity_map: Optional ``speaker_id → ambiguity_tag`` mapping from
            SpeakerLinker. When present, the tag is embedded into the
            description so the NetworkX ``entity.attrs`` carries it forward
            (Q2 decision: AMBIGUOUS label入图).

    Returns:
        List of ``(name, type, description)`` triples — one per distinct
        non-None ``speaker_id``. ``type`` is always ``"说话人"`` (DEFAULT_ENTITY_TYPES).
    """
    if not segment_speakers:
        return []
    amap = ambiguity_map or {}
    seen: dict[str, tuple[str, str, str]] = {}
    for _seg_idx, spk, _rec_id in segment_speakers:
        if spk is None:
            continue
        if spk in seen:
            continue
        ambiguity_tag = amap.get(spk)
        description = f"speaker label={spk}"
        if ambiguity_tag == "AMBIGUOUS":
            description += " (AMBIGUOUS — voiceprint merge below 0.7 threshold)"
        elif ambiguity_tag == "PENDING_REVIEW":
            description += " (PENDING_REVIEW — admin hold)"
        seen[spk] = (spk, "说话人", description)
    return list(seen.values())


__all__ = [
    "EntityExtractor",
    "ExtractedEntity",
    "ExtractedRelation",
    "ExtractionResult",
    "build_speaker_entities_from_segments",
]
