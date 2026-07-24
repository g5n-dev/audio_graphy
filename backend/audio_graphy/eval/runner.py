"""EvalRunner — runs metrics over a gold set against an EvalPipeline.

评估运行器：加载 gold set YAML → 并发跑每个 example 的 8 项指标 → 聚合。

Pipeline protocol (PRD §5.2):
    async def predict(gold: GoldExample) -> PredictedResult

Built-in pipelines:
- ``MockPipeline(precision=1.0)`` — echoes gold (M5 default; for smoke testing).
- ``RAGPipeline`` — calls real ``QueryService.search`` + entity extraction.

Position de-bias (M6):
    When ``position_debias=True`` (default), each LLM-judge metric is
    computed twice — once on the original retrieved context, once on the
    reversed context — and the mean is reported. Only applies to
    judge-dependent metrics (faithfulness / answer_relevance /
    factual_correctness). Retrieval / entity / edge / tag metrics are
    pure set operations and are not affected.

Concurrency: asyncio.Semaphore bound (default 4 from settings.eval_concurrency).
Error tolerance: per-example exceptions are captured into EvalExampleResult.error
  and the example is excluded from the aggregate mean.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from audio_graphy.eval.metrics.audio_graphy import (
    edge_precision_by_confidence,
    entity_f1,
    tag_accuracy,
)
from audio_graphy.eval.metrics.retrieval import context_precision_at_k, context_recall
from audio_graphy.eval.types import (
    EvalExampleResult,
    EvalRun,
    GoldExample,
    MetricResult,
    PredictedResult,
)
from audio_graphy.storage.graph_networkx import NetworkXGraphStore
from audio_graphy.storage.mysql_vector import MySQLVectorStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from audio_graphy.adapters.bundle import AdapterBundle
    from audio_graphy.config import Settings
    from audio_graphy.eval.judge import LLMJudge
    from audio_graphy.services.query import QueryService

logger = logging.getLogger(__name__)

_DEFAULT_K = 5

# Metric names that depend on LLM-judge output. Position de-bias only
# applies to these — retrieval / entity / edge / tag metrics are pure
# set operations and do not vary with context order.
_JUDGE_DEPENDENT_METRICS = frozenset({"faithfulness", "answer_relevance", "factual_correctness"})


# ============================================================
# Pipeline protocol + built-ins
# ============================================================


class EvalPipeline(Protocol):
    """Abstract pipeline: produces predictions for a gold example."""

    async def predict(self, gold: GoldExample) -> PredictedResult: ...


class MockPipeline:
    """Echoes gold back as the prediction — for testing metrics/reporter.

    Args:
        precision: 1.0 → echo gold (perfect score); 0.0 → empty prediction.
    """

    def __init__(self, precision: float = 1.0) -> None:
        self.precision = precision

    async def predict(self, gold: GoldExample) -> PredictedResult:
        if self.precision >= 1.0:
            return PredictedResult(
                query=gold.query,
                answer=gold.gold_answer,
                retrieved_context_ids=gold.gold_context_ids,
                entities=gold.gold_entities,
                edges=gold.gold_edges,
                tags=gold.gold_tags,
            )
        return PredictedResult(
            query=gold.query,
            answer="",
            retrieved_context_ids=(),
            entities=(),
            edges=(),
            tags=(),
        )

    def __repr__(self) -> str:
        return f"MockPipeline(precision={self.precision})"


class RAGPipeline:
    """Real pipeline that calls ``QueryService.search`` for each gold query.

    Replaces the M5 ``NotImplementedError`` stub. Each ``predict()`` call:
        1. Build a QueryRequest from ``gold.query``.
        2. Call ``QueryService.search()`` (dual-channel retrieval + rerank).
        3. Extract answer text + retrieved chunk_ids.
        4. Run ``core/extractor.py`` entity extraction on the answer to
           populate ``entities`` / ``edges``.
        5. Build ``PredictedResult``.

    Works in mock settings (mock adapters return deterministic output)
    as well as real settings (funASR + vLLM).

    Args:
        settings: Application settings (used to read working_dir).
        tenant_id: Tenant scope for the underlying QueryService call.
        user_id: Optional acting user ID (for audit attribution).
        bundle: AdapterBundle (used to build the EntityExtractor).
        session_factory: Async session maker.
        query_service: Pre-built QueryService (recommended for testing).
        graph_store: Pre-built NetworkXGraphStore (recommended for testing).
        vector_store: Pre-built MySQLVectorStore (recommended for testing).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        tenant_id: str,
        user_id: int | None,
        bundle: AdapterBundle,
        session_factory: async_sessionmaker[AsyncSession],
        query_service: QueryService | None = None,
        graph_store: NetworkXGraphStore | None = None,
        vector_store: MySQLVectorStore | None = None,
    ) -> None:
        self._settings = settings
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._bundle = bundle
        self._session_factory = session_factory
        self._query_service = query_service
        self._graph_store = graph_store
        self._vector_store = vector_store

    async def predict(self, gold: GoldExample) -> PredictedResult:
        """Run the real pipeline end-to-end for one gold example."""
        # 1. Lazily build QueryService if not injected.
        svc = self._query_service
        if svc is None:
            svc = await self._build_query_service()

        # 2. Retrieve + answer.
        top_k = 5
        result = await svc.search(
            tenant_id=self._tenant_id,
            query=gold.query,
            top_k=top_k,
            user_id=self._user_id,
        )
        answer_text = str(result.get("answer") or "")
        citations = result.get("citations") or []
        retrieved_ids = tuple(
            str(c.get("chunk_id")) for c in citations if c.get("chunk_id") is not None
        )

        # 3. Entity extraction on the answer.
        entities, edges = await self._extract_answer_entities(answer_text)

        # 4. Build tags from retrieval stats (preserves PredictedResult shape).
        tags: list[dict[str, str]] = []
        stats = result.get("retrieval_stats") or {}
        for key, value in stats.items():
            tags.append({"tag_path": f"retrieval.{key}", "value": str(value)})

        # 5. Inject retrieved_text for the faithfulness metric (PRD §5.3.1):
        # the LLM-as-judge needs a single context string to verify facts.
        retrieved_text = "\n".join(str(c.get("transcript_snippet") or "") for c in citations)
        if retrieved_text:
            tags.append({"tag_path": "retrieved_text", "value": retrieved_text})

        return PredictedResult(
            query=gold.query,
            answer=answer_text,
            retrieved_context_ids=retrieved_ids,
            entities=tuple(entities),
            edges=tuple(edges),
            tags=tuple(tags),
        )

    def __repr__(self) -> str:
        return f"RAGPipeline(tenant={self._tenant_id!r}, user_id={self._user_id!r})"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _build_query_service(self) -> QueryService:
        """Lazily construct a QueryService bound to this tenant."""
        from audio_graphy.services.query import QueryService
        from audio_graphy.storage.file_index import FileIndex

        if self._graph_store is None:
            self._graph_store = NetworkXGraphStore(
                self._settings.working_dir, tenant_id=self._tenant_id
            )
        if self._vector_store is None:
            # Caller forgot to inject — fail loudly so tests catch the gap.
            raise RuntimeError(
                "RAGPipeline requires either query_service or "
                "(vector_store + graph_store) to be provided."
            )
        file_index = FileIndex(self._settings.working_dir, tenant_id=self._tenant_id)
        return QueryService(
            session_factory=self._session_factory,
            bundle=self._bundle,
            vector_store=self._vector_store,
            graph_store=self._graph_store,
            file_index=file_index,
        )

    async def _extract_answer_entities(
        self,
        answer_text: str,
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, Any]]]:
        """Extract entities + edges from ``answer_text`` using GraphRAG prompts.

        Returns two tuples-of-tuples suitable for ``PredictedResult``
        (without the ``description`` / ``weight`` fields).
        """
        if not answer_text.strip():
            return [], []

        # Use a stripped-down EntityExtractor call: the prompts/templates
        # expect a chunk context. We bind a minimal prompt that just runs
        # one extraction round (no Gleaning) to keep eval cheap.
        try:
            from audio_graphy.core.extractor import EntityExtractor

            # Build a minimal extraction prompt inline. We avoid loading
            # the heavy entity_zh.md template — eval only needs *something*
            # to count against the gold entities.
            minimal_prompt = (
                "请从下面的回答中抽取核心实体（人/产品/地点/数字/方案）。\n"
                "使用 GraphRAG 分隔符格式输出：\n"
                '("实体"{td}名称{td}类型{td}描述){rd}{cd}'
            )
            from audio_graphy.core.types import (
                COMPLETION_DELIMITER,
                RECORD_DELIMITER,
                TUPLE_DELIMITER,
            )

            template = (
                minimal_prompt.format(
                    td=TUPLE_DELIMITER, rd=RECORD_DELIMITER, cd=COMPLETION_DELIMITER
                )
                + "\n\n输入文本:\n{input_text}"
            )

            extractor = EntityExtractor(
                self._bundle,
                prompt_template=template,
                gleaning_rounds=0,
            )
            # Use a synthetic chunk_id=0 / recording_id=0 — eval does not
            # care about provenance, only the (name, type) tuples.
            result = await extractor.extract_from_chunk(
                chunk_id=0,
                chunk_text=answer_text,
                recording_id=0,
            )
            ents = [(e.name, e.type) for e in result.entities]
            eds = [
                (r.source_name, r.relation, r.target_name, r.confidence) for r in result.relations
            ]
            return ents, eds
        except Exception as exc:
            logger.warning("RAGPipeline entity extraction failed: %s — returning empty", exc)
            return [], []


# ============================================================
# Runner
# ============================================================


class EvalRunner:
    """Runs metrics over a gold set against a pipeline.

    Args:
        gold_set_path: Path to a YAML file containing a list of gold examples.
        pipeline: Any object implementing ``EvalPipeline``.
        judge: Optional LLMJudge; when ``None``, faithfulness / answer_relevance
            / factual_correctness are skipped (recorded as 0.0 with
            ``details.skipped=True``).
        settings: Used to read ``eval_concurrency`` and ``eval_position_debias``.
            If ``None``, uses 4 + ``position_debias=True``.
        k: Cutoff for ``context_precision_at_k`` (default 5).
        config_snapshot: Optional dict merged into ``EvalRun.config``.
        position_debias: When ``True``, LLM-judge metrics are run twice
            (original + reversed retrieved context) and the mean is taken.
            Defaults to the value in ``settings.eval_position_debias`` if
            ``settings`` is provided, else ``True``.
    """

    def __init__(
        self,
        *,
        gold_set_path: Path,
        pipeline: EvalPipeline,
        judge: LLMJudge | None = None,
        settings: Settings | None = None,
        k: int = _DEFAULT_K,
        config_snapshot: dict[str, str] | None = None,
        position_debias: bool | None = None,
        entity_fuzzy_threshold: float | None = None,
        voiceprint_eer_enabled: bool = False,
        diarization_der_enabled: bool = False,
    ) -> None:
        self._gold_set_path = Path(gold_set_path)
        self._pipeline = pipeline
        self._judge = judge
        self._k = k
        concurrency = settings.eval_concurrency if settings is not None else 4
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._config_snapshot = dict(config_snapshot or {})
        self._config_snapshot.setdefault(
            "pipeline",
            type(pipeline).__name__ + f"(precision={getattr(pipeline, 'precision', 'n/a')})",
        )
        self._config_snapshot.setdefault("k", str(k))
        self._config_snapshot.setdefault("judge", "enabled" if judge is not None else "disabled")
        # Position de-bias: explicit arg > settings > default True.
        if position_debias is None:
            self._position_debias = (
                bool(getattr(settings, "eval_position_debias", True))
                if settings is not None
                else True
            )
        else:
            self._position_debias = bool(position_debias)
        self._config_snapshot["position_debias"] = str(self._position_debias)
        # Entity fuzzy threshold: explicit arg > settings.entity_fuzzy_threshold > 0.85.
        if entity_fuzzy_threshold is None:
            self._entity_fuzzy_threshold = (
                float(getattr(settings, "entity_fuzzy_threshold", 0.85))
                if settings is not None
                else 0.85
            )
        else:
            self._entity_fuzzy_threshold = float(entity_fuzzy_threshold)
        self._config_snapshot["entity_fuzzy_threshold"] = str(self._entity_fuzzy_threshold)
        # M7 — voiceprint EER + diarization DER opt-in flags.
        self._voiceprint_eer_enabled = bool(voiceprint_eer_enabled)
        self._diarization_der_enabled = bool(diarization_der_enabled)
        self._config_snapshot["voiceprint_eer"] = (
            "enabled" if self._voiceprint_eer_enabled else "disabled"
        )
        self._config_snapshot["diarization_der"] = (
            "enabled" if self._diarization_der_enabled else "disabled"
        )

    async def run(self) -> EvalRun:
        started_at = datetime.now(UTC).isoformat()
        run_id = uuid.uuid4().hex[:12]
        examples = self._load_gold_set()
        tasks = [self._eval_one(ex, idx) for idx, ex in enumerate(examples)]
        per_example = tuple(await asyncio.gather(*tasks))
        aggregate = self._aggregate(per_example)
        finished_at = datetime.now(UTC).isoformat()
        return EvalRun(
            run_id=run_id,
            gold_set_path=str(self._gold_set_path),
            started_at=started_at,
            finished_at=finished_at,
            config=self._config_snapshot,
            aggregate_metrics=aggregate,
            per_example=per_example,
        )

    # ----------------------------------------------------------
    # Per-example evaluation
    # ----------------------------------------------------------
    async def _eval_one(self, gold: GoldExample, idx: int) -> EvalExampleResult:
        example_id = f"ex-{idx + 1:03d}"
        try:
            async with self._semaphore:
                pred = await self._pipeline.predict(gold)
        except Exception as exc:
            logger.error("Pipeline crashed on %s: %s", example_id, exc)
            return EvalExampleResult(example_id=example_id, metrics=(), error=repr(exc))

        try:
            metrics = await self._compute_metrics(gold, pred)
        except Exception as exc:
            logger.error("Metric failed on %s: %s", example_id, exc)
            return EvalExampleResult(example_id=example_id, metrics=(), error=repr(exc))

        return EvalExampleResult(example_id=example_id, metrics=tuple(metrics), error=None)

    async def _compute_metrics(
        self, gold: GoldExample, pred: PredictedResult
    ) -> list[MetricResult]:
        # Retrieval metrics (no LLM).
        results: list[MetricResult] = [
            context_precision_at_k(gold, pred, k=self._k),
            context_recall(gold, pred),
        ]

        # AudioGraphy-specific metrics (no LLM).
        # Entity F1 is computed twice — strict (threshold=1.0) and fuzzy
        # (threshold=settings.entity_fuzzy_threshold) — so the aggregate
        # report shows both for diagnosing near-dup clustering quality.
        results.extend(
            [
                entity_f1(gold, pred, fuzzy_threshold=1.0),
                entity_f1(gold, pred, fuzzy_threshold=self._entity_fuzzy_threshold),
                edge_precision_by_confidence(gold, pred),
                tag_accuracy(gold, pred),
            ]
        )

        # LLM-backed metrics — skipped when judge is None.
        if self._judge is None:
            for name in ("faithfulness", "answer_relevance", "factual_correctness"):
                results.append(
                    MetricResult(
                        name=name,
                        value=0.0,
                        denominator=0,
                        details={"skipped": True},
                    )
                )
        else:
            from audio_graphy.eval.metrics.generation import (
                answer_relevance,
                factual_correctness,
                faithfulness,
            )

            if self._position_debias:
                results.append(
                    await self._judge_with_debias(faithfulness, gold, pred, name="faithfulness")
                )
                results.append(
                    await self._judge_with_debias(
                        answer_relevance, gold, pred, name="answer_relevance"
                    )
                )
                results.append(
                    await self._judge_with_debias(
                        factual_correctness, gold, pred, name="factual_correctness"
                    )
                )
            else:
                results.append(await faithfulness(gold, pred, self._judge))
                results.append(await answer_relevance(gold, pred, self._judge))
                results.append(await factual_correctness(gold, pred, self._judge))

        # M7 — Phase 2 metrics: voiceprint EER + diarization DER.
        # Only emitted when explicitly enabled AND the gold example carries
        # the corresponding annotation (speaker trials / ref diarization).
        results.extend(await self._compute_phase2_metrics(gold))

        return results

    async def _compute_phase2_metrics(
        self,
        gold: GoldExample,
    ) -> list[MetricResult]:
        """M7 Phase 2 metrics — voiceprint EER + diarization DER.

        Both are gated by an explicit opt-in flag on EvalRunner, and
        require the gold example to carry speaker annotations in its
        ``metadata`` dict:

            metadata["voiceprint_trials"]:
                list of (enrollment_path, test_path, "1"|"0") triples.
            metadata["reference_rttm"] / metadata["hypothesis_rttm"]:
                paths to RTTM files.

        Both are best-effort — missing data → skipped (details["skipped"]=True).
        """
        out: list[MetricResult] = []

        if self._voiceprint_eer_enabled:
            trials = gold.metadata.get("voiceprint_trials", "")
            if trials:
                # Parse "path1 path2 1\npath3 path4 0\n..." into two cosine
                # buckets. We can't run the real adapter here (no bundle),
                # so this metric is a placeholder that records the trial
                # count; the actual extraction is left to a dedicated
                # eval script that builds the adapter and calls
                # ``voiceprint_eer_from_trials`` directly.
                from audio_graphy.eval.metrics.voiceprint import voiceprint_eer_metric

                # Tokenise the trials string into same/diff buckets.
                # We expect precomputed cosines when set by tests; otherwise
                # the entry is left as zero (skipped via denominator=0).
                #
                # Format (per "row"): "cos <value> <label>" where label is
                # "1" (same speaker) or "0" (different). Rows may be
                # separated by newlines OR by whitespace (YAML collapses
                # multi-line single-quoted strings to a single line). We
                # detect rows by looking for the literal token "cos" and
                # reading the next 2 tokens.
                same_cos: list[float] = []
                diff_cos: list[float] = []
                tokens = (
                    trials.replace("\\n", " ")  # literal "\n" (YAML single-quoted)
                    .replace("\n", " ")  # actual newline
                    .split()
                )
                i = 0
                while i + 2 < len(tokens):
                    if tokens[i] == "cos":
                        try:
                            value = float(tokens[i + 1])
                            label = tokens[i + 2]
                        except ValueError:
                            i += 1
                            continue
                        if label == "1":
                            same_cos.append(value)
                        else:
                            diff_cos.append(value)
                        i += 3
                    else:
                        i += 1
                out.append(voiceprint_eer_metric(same_cos, diff_cos))
            else:
                out.append(
                    MetricResult(
                        name="voiceprint_eer",
                        value=0.0,
                        denominator=0,
                        details={"skipped": True},
                    )
                )

        if self._diarization_der_enabled:
            ref_path = gold.metadata.get("reference_rttm", "")
            hyp_path = gold.metadata.get("hypothesis_rttm", "")
            if ref_path and hyp_path:
                from audio_graphy.eval.metrics.diarization import (
                    diarization_der_metric,
                    parse_rttm,
                )

                try:
                    ref_segs = parse_rttm(ref_path)
                    hyp_segs = parse_rttm(hyp_path)
                    out.append(diarization_der_metric(hyp_segs, ref_segs))
                except Exception as exc:
                    logger.warning("DER computation failed: %s", exc)
                    out.append(
                        MetricResult(
                            name="diarization_der",
                            value=0.0,
                            denominator=0,
                            details={"skipped": True, "error": str(exc)},
                        )
                    )
            else:
                out.append(
                    MetricResult(
                        name="diarization_der",
                        value=0.0,
                        denominator=0,
                        details={"skipped": True},
                    )
                )

        return out

    async def _judge_with_debias(
        self,
        metric_fn: Any,
        gold: GoldExample,
        pred: PredictedResult,
        *,
        name: str,
    ) -> MetricResult:
        """Run a judge-dependent metric twice (original + reversed context).

        Reverses the ``retrieved_text`` tag inside ``pred.tags`` to flip
        the context order the LLM judge sees, then takes the mean of the
        two scores. ``details.debiased=True`` is set on the result.

        Retrieval / entity / edge / tag metrics are NOT debiased (they
        are pure set operations and order-invariant).
        """
        # 1. Original order.
        m_orig = await metric_fn(gold, pred, self._judge)
        # 2. Reversed context — reverse the retrieved_text tag.
        pred_rev = self._reverse_retrieved_text(pred)
        m_rev = await metric_fn(gold, pred_rev, self._judge)
        # 3. Mean.
        mean_value = (float(m_orig.value) + float(m_rev.value)) / 2.0
        merged_details: dict[str, float | int | str] = dict(m_orig.details)
        merged_details["debiased"] = True
        merged_details["value_original"] = float(m_orig.value)
        merged_details["value_reversed"] = float(m_rev.value)
        return MetricResult(
            name=name,
            value=mean_value,
            denominator=m_orig.denominator,
            details=merged_details,
        )

    @staticmethod
    def _reverse_retrieved_text(pred: PredictedResult) -> PredictedResult:
        """Return a copy of ``pred`` with the retrieved_text tag reversed.

        If no ``retrieved_text`` tag is present, returns ``pred`` unchanged
        (no-op rather than raising — the metric will surface its own
        empty_context reason).
        """
        from dataclasses import replace

        found = False
        new_tags: list[dict[str, str]] = []
        for t in pred.tags:
            if t.get("tag_path") == "retrieved_text":
                # Reverse line order (keeps each snippet intact, flips ranking).
                lines = str(t.get("value", "")).split("\n")
                new_tags.append({"tag_path": "retrieved_text", "value": "\n".join(reversed(lines))})
                found = True
            else:
                new_tags.append(dict(t))
        if not found:
            return pred
        return replace(pred, tags=tuple(new_tags))

    # ----------------------------------------------------------
    # Aggregation
    # ----------------------------------------------------------
    @staticmethod
    def _aggregate(per_example: tuple[EvalExampleResult, ...]) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        for ex in per_example:
            if ex.error is not None:
                continue
            for m in ex.metrics:
                buckets.setdefault(m.name, []).append(m.value)
        return {name: sum(vals) / len(vals) for name, vals in buckets.items() if vals}

    # ----------------------------------------------------------
    # Gold set loading
    # ----------------------------------------------------------
    def _load_gold_set(self) -> list[GoldExample]:
        if not self._gold_set_path.is_file():
            raise FileNotFoundError(f"gold set not found: {self._gold_set_path}")
        raw = yaml.safe_load(self._gold_set_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"gold set must be a YAML list, got {type(raw).__name__}")
        return [self._gold_from_dict(item, i) for i, item in enumerate(raw)]

    @staticmethod
    def _gold_from_dict(item: object, idx: int) -> GoldExample:
        if not isinstance(item, dict):
            raise ValueError(f"gold[{idx}] is not a mapping: {item!r}")
        try:
            return GoldExample(
                query=str(item["query"]),
                gold_answer=str(item["gold_answer"]),
                gold_context_ids=tuple(str(x) for x in item.get("gold_context_ids", [])),
                gold_entities=tuple((str(t), str(y)) for t, y in item.get("gold_entities", [])),
                gold_edges=tuple(
                    (str(s), str(r), str(d), str(c))  # type: ignore[misc]
                    for s, r, d, c in item.get("gold_edges", [])
                ),
                gold_tags=tuple(
                    {str(k): str(v) for k, v in dict(t).items()} for t in item.get("gold_tags", [])
                ),
                recording_id=(str(item["recording_id"]) if item.get("recording_id") else None),
                metadata={str(k): str(v) for k, v in dict(item.get("metadata", {})).items()},
            )
        except KeyError as exc:
            raise ValueError(f"gold[{idx}] missing required key: {exc}") from exc


__all__ = ["EvalPipeline", "EvalRunner", "MockPipeline", "RAGPipeline"]
