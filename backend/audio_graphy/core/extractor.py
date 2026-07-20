"""Entity extractor — GraphRAG delimiter protocol parsing + Gleaning + Chinese normalisation.

Pipeline per chunk:
    1. Build extraction prompt (with GraphRAG delimiters + entity types + few-shot)
    2. Call strong_llm.complete() with cache_key
    3. Parse LLM output: split by delimiters → ExtractedEntity[] + ExtractedRelation[]
    4. Gleaning: ask LLM if anything was missed → supplement extraction
    5. Chinese entity normalisation: alias table + edit-distance clustering

Parser strategy (architecture §1.4):
    - Primary: split by TUPLE_DELIMITER / RECORD_DELIMITER / COMPLETION_DELIMITER
    - Fallback: CSV-style quoted fields (for mock LLM compatibility)
    - Lenient regex: extract partial matches, mark parse_success=False

LLM cache (architecture §4.3):
    - Layer 1: adapter _cache (in-process, managed by adapter)
    - Layer 2: file_index kv_store_llm_response_cache.json (persistent)
    - Core module checks Layer 2 before calling adapter
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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

if TYPE_CHECKING:
    from audio_graphy.storage.file_index import FileIndex

logger = logging.getLogger(__name__)

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
        entity_types: Domain entity types tuple.
        max_gleaning_retry: Max retries for Gleaning LLM call (default 2).
        file_index: Optional FileIndex for LLM response cache (Layer 2).
        aliases: Entity name alias table for Chinese normalisation.
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        *,
        prompt_template: str,
        gleaning_rounds: int = 1,
        entity_types: tuple[str, ...] = DEFAULT_ENTITY_TYPES,
        max_gleaning_retry: int = 2,
        file_index: FileIndex | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self._bundle = bundle
        self._prompt_template = prompt_template
        self._gleaning_rounds = gleaning_rounds
        self._entity_types = entity_types
        self._max_gleaning_retry = max_gleaning_retry
        self._file_index = file_index
        self._aliases = aliases if aliases is not None else dict(_DEFAULT_ALIASES)

    async def extract_from_chunk(
        self,
        chunk_id: int,
        chunk_text: str,
        recording_id: int,
    ) -> ExtractionResult:
        """Extract entities and relations from a single chunk.

        Args:
            chunk_id: Chunk database ID.
            chunk_text: Chunk text content.
            recording_id: Recording ID for provenance.

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
        prompt = self._build_prompt(chunk_text)
        response = await self._cached_complete(prompt)
        entities, relations, parse_success = self._parse_llm_output(
            response.text, chunk_id, recording_id
        )

        # Step 2: Gleaning rounds
        actual_gleaning_rounds = 0
        for _round_idx in range(self._gleaning_rounds):
            new_entities, new_relations = await self._glean(
                entities, relations, chunk_text, chunk_id, recording_id
            )
            actual_gleaning_rounds += 1

            if not new_entities and not new_relations:
                # No new findings — early termination
                break

            entities.extend(new_entities)
            relations.extend(new_relations)

        # Step 3: Chinese entity normalisation
        entities = self._normalize_entities(entities)
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
    ) -> list[ExtractionResult]:
        """Extract from multiple chunks concurrently.

        Args:
            chunks: Sequence of (chunk_id, text, recording_id) tuples.
            concurrency: Max concurrent LLM calls.

        Returns:
            List of ExtractionResult (one per chunk, in input order).
        """
        semaphore = asyncio.Semaphore(concurrency)

        async def _extract_with_limit(chunk_id: int, text: str, rec_id: int) -> ExtractionResult:
            async with semaphore:
                return await self.extract_from_chunk(chunk_id, text, rec_id)

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

        Returns:
            Tuple of (new_entities, new_relations) from Gleaning.
        """
        prompt = self._build_gleaning_prompt(entities, relations, chunk_text)

        # Retry logic for Gleaning LLM call
        response: LLMResponse | None = None
        for attempt in range(self._max_gleaning_retry + 1):
            try:
                response = await self._cached_complete(prompt)
                break
            except Exception as exc:
                if attempt < self._max_gleaning_retry:
                    logger.warning(
                        "Gleaning LLM call failed (attempt %d/%d): %s — retrying",
                        attempt + 1,
                        self._max_gleaning_retry + 1,
                        exc,
                    )
                    await asyncio.sleep(2**attempt * 0.1)  # Exponential backoff
                else:
                    logger.warning("Gleaning LLM call failed after retries: %s", exc)
                    return [], []

        if response is None:
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

    def _normalize_entities(self, entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
        """Normalise entity names using alias table and edit-distance clustering.

        Args:
            entities: Raw extracted entities.

        Returns:
            Entities with normalised names.
        """
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
    # LLM cache (dual-layer)
    # ------------------------------------------------------------------

    async def _cached_complete(self, prompt: str) -> LLMResponse:
        """Call LLM with dual-layer cache (file_index Layer 2 + adapter Layer 1).

        Args:
            prompt: Formatted prompt string.

        Returns:
            LLMResponse (cached=True if cache hit).
        """
        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
        model = self._bundle.strong_llm.model
        cache_key = self._compute_cache_key(model, messages)

        # Layer 2: Check file_index persistent cache
        if self._file_index is not None:
            cached_text = await self._file_index.get_llm_cache(cache_key)
            if cached_text is not None:
                return LLMResponse(
                    text=cached_text,
                    model=model,
                    prompt_hash=cache_key,
                    cached=True,
                    usage={},
                )

        # Layer 1 + API: Call adapter (adapter checks its own _cache)
        response = await self._bundle.strong_llm.complete(
            messages=messages,
            cache_key=cache_key,
        )

        # Store in Layer 2 (file_index)
        if self._file_index is not None and not response.cached:
            await self._file_index.set_llm_cache(cache_key, response.text)

        return response

    @staticmethod
    def _compute_cache_key(model: str, messages: Sequence[dict[str, str]]) -> str:
        """Compute LLM cache key = MD5(model, messages).

        Same formula as MockLLMAdapter.compute_prompt_hash.

        Args:
            model: LLM model name.
            messages: Chat messages.

        Returns:
            MD5 hex digest string.
        """
        payload = json.dumps(
            {"model": model, "messages": list(messages)},
            ensure_ascii=False,
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()
