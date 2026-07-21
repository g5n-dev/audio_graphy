# AudioGraphy M5 Evaluation Guide

> 评估指南 — covers the 8-metric eval subsystem shipped in M5: gold set
> YAML schema, CLI usage, LLM-as-judge setup, output format, and how to
> extend with your own `EvalPipeline`.

| Section | What you get |
|---|---|
| [§1 Overview](#1-overview) | what / why / scope |
| [§2 Quick start](#2-quick-start) | one-command smoke run |
| [§3 Gold set YAML](#3-gold-set-yaml-format) | schema + example |
| [§4 Metrics](#4-metrics) | 8 metrics: formula + range + interpretation |
| [§5 LLM-as-judge](#5-llm-as-judge) | prompts, caching, model override |
| [§6 CLI reference](#6-cli-reference) | all flags |
| [§7 Output format](#7-output-format) | Markdown + JSON layout |
| [§8 Custom pipelines](#8-writing-your-own-evalpipeline) | MockPipeline / RAGPipeline stubs |
| [§9 Troubleshooting](#9-troubleshooting) | common failure modes |

---

## 1. Overview

M5 ships an **in-tree evaluation subsystem** (`audio_graphy.eval`) that lets
operators, QA, and CI measure pipeline quality against a curated gold set.

- **8 metrics** (5 RAG-standard + 3 AudioGraphy-specific), all pure functions.
- **LLM-as-judge** for 3 generation metrics — reuses `LLMOpenAIAdapter(strong)`,
  no new model service needed.
- **Zero new pip dependencies** — the only extra runtime dep is `pyyaml`,
  already present since M3.
- **CLI** entry: `python -m audio_graphy.eval`.
- **Markdown + JSON** reports for human + machine consumption.

Out-of-scope for M5:
- Real RAG pipeline integration (M6 — `EvalPipeline` protocol ships, but
  `RAGPipeline` is a stub).
- Cross-run trend storage (M6+ — for now, JSON files are the source of truth).
- Prometheus metrics export (not on roadmap; Q5 locked).

---

## 2. Quick start

### 2.1 Smoke run (no GPU, no LLM)

```bash
cd backend
python -m audio_graphy.eval \
  --gold-set ../examples/eval/smoke.yaml \
  --report-dir reports/ \
  --no-judge
```

Output (on stderr):

```
⚠  Using MockPipeline(precision=1.0) — for real RAG evaluation wait for M6. ...
Eval complete: 10/10 ok → reports/eval-<run_id>.md
```

Two files written to `reports/`:
- `eval-<run_id>.md` — human-readable Markdown report
- `eval-<run_id>.json` — machine-readable JSON (dataclass `asdict`)

### 2.2 Full run (with LLM judge, requires vLLM strong)

1. Start vLLM strong:

   ```bash
   docker compose --profile real up -d vllm-strong
   ```

2. Run without `--no-judge`:

   ```bash
   ADAPTER_LLM_MODE=real python -m audio_graphy.eval \
     --gold-set ../examples/eval/smoke.yaml \
     --report-dir reports/
   ```

   The CLI will lazily build the adapter bundle and wire the strong LLM into
   `LLMJudge`. Each example triggers ~3 LLM calls (extract_facts,
   judge_faithfulness, judge_relevance) — see [§5.3](#53-cost) for the cost model.

---

## 3. Gold set YAML format

The gold set is a YAML **list of mappings**. Each item has these fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | str | ✅ | User question / 用户提问 |
| `gold_answer` | str | ✅ | Reference answer |
| `gold_context_ids` | list[str] | — | Ground-truth chunk IDs for retrieval metrics |
| `gold_entities` | list[[str, str]] | — | `(entity_text, entity_type)` tuples |
| `gold_edges` | list[[str, str, str, str]] | — | `(src, rel, dst, confidence)`; confidence ∈ {`EXTRACTED`, `INFERRED`, `AMBIGUOUS`} |
| `gold_tags` | list[dict] | — | `{"tag_path": ..., "value": ...}` entries |
| `recording_id` | str | — | Optional audio recording ID |
| `metadata` | dict | — | Free-form metadata (tenant, scenario, etc.) |

### 3.1 Example

```yaml
- query: "CS75 Plus 七月优惠多少？"
  gold_answer: "5 万元现金优惠 + 2 年免息分期。"
  gold_context_ids: ["chunk-001", "chunk-004"]
  gold_entities:
    - ["CS75 Plus", "车型"]
    - ["5万", "价格方案"]
  gold_edges:
    - ["坐席", "推荐", "CS75 Plus", "EXTRACTED"]
    - ["CS75 Plus", "搭配", "2年免息", "INFERRED"]
  gold_tags:
    - {tag_path: "接待.价格.优惠", value: "5万"}
    - {tag_path: "接待.金融.免息", value: "2年"}
  metadata: {scenario: "sales", tenant: "default"}
```

The bundled `examples/eval/smoke.yaml` has 10 examples covering easy / hard
retrieval, entity-heavy / edge-heavy / tag-heavy scenarios, empty gold, and
multi-tenant cases. Use it as a template.

### 3.2 Schema notes

- All `*_entities` / `*_edges` fields use lists of lists (YAML friendly); the
  loader converts them to tuples internally.
- The 4th element of each gold edge **must** be one of `EXTRACTED`, `INFERRED`,
  `AMBIGUOUS`. Anything else fails at metric time with an out-of-Literal
  warning.
- Empty `gold_*` lists trigger the `denominator_zero=True` code path in the
  relevant metric (see [§4](#4-metrics)).

---

## 4. Metrics

8 metrics, all returning `MetricResult(name, value, denominator, details)` with
`value ∈ [0.0, 1.0]` (1.0 = best).

### 4.1 Retrieval (no LLM)

| Metric | Formula | Edge cases |
|---|---|---|
| **context_precision_at_k** | `|gold ∩ retrieved[:k]| / min(k, len(gold))` | gold empty OR k ≤ 0 → 0.0, `denominator_zero=True` |
| **context_recall** | `|gold ∩ retrieved_all| / len(gold)` | gold empty → 0.0, `denominator_zero=True` |

`k` defaults to 5 (CLI `--k`). Retrieved IDs are taken from
`PredictedResult.retrieved_context_ids` in rank order.

### 4.2 Generation (LLM-backed)

| Metric | Formula | Edge cases |
|---|---|---|
| **faithfulness** | `supported_facts / total_facts` (extracted from answer) | empty answer → 0.0 `empty_answer`; empty context → 0.0 `empty_context`; no facts → 0.0 `no_facts_extracted` |
| **answer_relevance** | `judge.judge_relevance(query, answer) ∈ {0.0, 0.5, 1.0}` | empty answer → 0.0 |
| **factual_correctness** | `F1(precision, recall)` over fact sets from answer vs gold_answer | both empty → **1.0** `both_empty` (PRD §5.3.3) |

The `retrieved_text` is read from `PredictedResult.tags` (key `"retrieved_text"`).
Pipelines that want a meaningful faithfulness score must stamp this tag.

### 4.3 AudioGraphy-specific (no LLM)

| Metric | Formula | Notes |
|---|---|---|
| **entity_f1** | F1 over `{(normalized_entity_text, entity_type)}` | both sets empty → **1.0** (PRD §5.3.3) |
| **edge_precision_by_confidence** | macro-mean of per-layer precision across `EXTRACTED` / `INFERRED` / `AMBIGUOUS` | layers with empty pred are excluded from macro; all empty → 0.0 `all_layers_empty` |
| **tag_accuracy** | `(# path+value matches) / len(gold_tags)` after NFKC + lowercase normalization | gold_tags empty → 0.0 `denominator_zero` |

Normalization (`_norm`): `unicodedata.normalize("NFKC", s).strip().lower()`.
This handles fullwidth-halfwidth variants (`｢ＣＳ７５｣` → `cs75`) and
case differences.

### 4.4 Aggregation

The aggregate metric in the report is the **arithmetic mean** of each metric
across non-errored examples:

```python
aggregate[name] = mean(value for ex in per_example if not ex.error)
```

Errored examples are excluded; this prevents a single pipeline crash from
dragging all metrics to 0.0.

---

## 5. LLM-as-judge

### 5.1 Prompts

Three prompt templates ship in `audio_graphy/eval/prompts/`:

| File | Purpose | Output format |
|---|---|---|
| `extract_facts.txt` | Extract atomic facts from a paragraph | `- fact1\n- fact2\n...` |
| `judge_faithfulness.txt` | Judge each fact against retrieved context | JSON-per-line `{"id": N, "supported": bool}` |
| `judge_relevance.txt` | Score answer relevance | single float ∈ {0.0, 0.5, 1.0} |

All three accept `{text}` / `{context}` / `{numbered_facts}` / `{query}` /
`{answer}` placeholders via `str.format`. Modify at your own risk — the
parsers expect the documented output shape.

### 5.2 Caching

Each LLM call constructs a cache key:

```
cache_key = MD5(method_name + "|" + arg1 + "|" + arg2 + ...)
```

The same `(method, args)` reuses the cached response within one process via
`LLMOpenAIAdapter`'s built-in cache. This means:

- `extract_facts("answer text")` called twice in one run → 1 HTTP request.
- `judge_faithfulness(ctx, facts)` → cache key includes the full fact list.
- `judge_relevance(query, answer)` → cache key on (query, answer).

### 5.3 Cost

Each example without cache hits triggers **~3 LLM calls**:
- `extract_facts(pred.answer)`
- `judge_faithfulness(retrieved_text, facts)`
- `judge_relevance(query, answer)`
- (Plus `extract_facts(gold.gold_answer)` for `factual_correctness`.)

Gold answers in `factual_correctness` cache well across examples (the
same gold answer appears in many examples within a tenant), so the
realistic upper bound for N=100 is ~250 calls, not 400.

### 5.4 Configuration

| Env var | Default | Purpose |
|---|---|---|
| `JUDGE_LLM_MODEL` | empty | Override judge model; empty → fallback to `LLM_STRONG_MODEL` |
| `EVAL_CONCURRENCY` | `4` | `asyncio.Semaphore` bound for parallel example evaluation |

CLI override: `--judge-llm <model-name>` (highest priority).

### 5.5 Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Judge returns 0.0 for everything | LLM misbehaving or prompt malformed | Check `--report-dir/eval-*.md` "Per-Example Highlights" + WARNING logs |
| `details.skipped=True` on faithfulness | `--no-judge` was passed OR judge init failed | Drop `--no-judge`; check adapter bundle startup |
| `factual_correctness=1.0` with `reason="both_empty"` | Both gold and pred answers had no extractable facts | Verify `gold_answer` is non-trivial; consider tightening the prompt |

---

## 6. CLI reference

```bash
python -m audio_graphy.eval --help
```

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `--gold-set` | Path | **required** | Path to gold set YAML |
| `--report-dir` | Path | `reports/` | Output directory (auto-created) |
| `--judge-llm` | str | empty | Override judge LLM model name |
| `--no-judge` | flag | false | Skip LLM-dependent metrics |
| `--pipeline` | `mock` \| `rag` | `mock` | Pipeline (rag is M6 — M5 returns exit 2) |
| `--k` | int | `5` | Cutoff for `context_precision_at_k` |

### 6.1 Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (per-example errors are in the report, not the exit code) |
| 2 | Argument error / gold set missing / `--pipeline rag` in M5 |
| 70 | Uncaught exception during evaluation |

---

## 7. Output format

### 7.1 Markdown

Top-down:

1. **Header**: Run ID, started/finished ISO 8601 timestamps, example counts
   (ok / errored), and a config dump (pipeline, judge, k, model).
2. **MockPipeline warning banner**: appears when `config["pipeline"]` mentions
   `MockPipeline` — alerts the reader that metrics are baseline upper bounds,
   not real scores.
3. **Aggregate Metrics table**: arithmetic mean per metric, sorted by name,
   values formatted to 3 decimal places.
4. **Per-Example Highlights**: top-5 worst examples sorted by `faithfulness`
   (or `context_precision_at_5` when judge was skipped).
5. **Errors**: table of errored examples with truncated (120 char) error
   strings; absent if no errors.

### 7.2 JSON

`json.dumps(asdict(eval_run), ensure_ascii=False, indent=2)` — fully
round-trippable. Tuples become lists; dicts preserved.

Top-level keys: `run_id`, `gold_set_path`, `started_at`, `finished_at`,
`config`, `aggregate_metrics`, `per_example` (list of `{example_id, metrics,
error}`).

---

## 8. Writing your own EvalPipeline

Implement the `EvalPipeline` Protocol:

```python
from audio_graphy.eval.types import GoldExample, PredictedResult


class MyRAGPipeline:
    """Example: wire a real RAG stack into the eval runner."""

    def __init__(self, query_service, retrieval_service) -> None:
        self._query = query_service
        self._retrieve = retrieval_service

    async def predict(self, gold: GoldExample) -> PredictedResult:
        # 1. Retrieve chunks
        chunks = await self._retrieve.search(gold.query, top_k=5)
        retrieved_ids = tuple(c.id for c in chunks)
        retrieved_text = "\n\n".join(c.text for c in chunks)

        # 2. Generate answer
        answer = await self._query.answer(gold.query, context=retrieved_text)

        # 3. Extract entities / edges / tags (use your real extractors)
        entities = ...
        edges = ...
        tags = (
            {"tag_path": "retrieved_text", "value": retrieved_text},
            *other_tags,
        )

        return PredictedResult(
            query=gold.query,
            answer=answer,
            retrieved_context_ids=retrieved_ids,
            entities=entities,
            edges=edges,
            tags=tags,
        )
```

Then use the runner directly:

```python
import asyncio
from audio_graphy.eval.runner import EvalRunner

runner = EvalRunner(
    gold_set_path=Path("examples/eval/smoke.yaml"),
    pipeline=MyRAGPipeline(...),
    judge=None,  # or LLMJudge(llm=strong_llm)
)
run = asyncio.run(runner.run())
```

### 8.1 Why `MockPipeline` is the M5 default

The CLI ships with `MockPipeline(precision=1.0)` as the default because real
RAG wiring lands in M6. **All metrics on the default run are baseline upper
bounds (mostly 1.0)** — the banner in Markdown makes this explicit. To get
real scores, implement `EvalPipeline` per [§8](#8-writing-your-own-evalpipeline).

---

## 9. Troubleshooting

### 9.1 `pytest tests/eval/` fails on import

**Symptom**: `ModuleNotFoundError: No module named 'audio_graphy.eval'`.

**Cause**: running pytest from outside `backend/`.

**Fix**: `cd backend && pytest tests/eval/`. The eval subpackage is only
on `sys.path` when `backend/` is the working directory.

### 9.2 Judge returns empty fact list every time

**Symptom**: `faithfulness=0.0` with `reason="no_facts_extracted"` for
every example.

**Cause**: The LLM is returning non-bulleted output, or the prompt template
was modified.

**Fix**: Inspect the raw LLM response (add a logger breakpoint in
`audio_graphy/eval/judge.py::_call_llm`). The `_parse_fact_list` helper
strips `- ` / `1. ` / `* ` prefixes; if your LLM emits something else,
either tweak the prompt or add a custom parser.

### 9.3 `Entity F1` is suspiciously low for Chinese entity variants

**Symptom**: Predictions like `CS75 Plus` vs gold `CS75PLUS` count as FN.

**Cause**: M5 uses strict set equality after NFKC + lowercase (no character
Jaccard, no jieba tokenization). This is a deliberate trade-off documented
in `docs/m5-architecture.md` appendix A.2.

**Fix**: M6 will revisit this with optional Chinese tokenizers. For now,
normalize entity strings in your pipeline's extraction step.

### 9.4 CLI exits with code 70

**Symptom**: `error: evaluation crashed: ...` on stderr, exit 70.

**Cause**: Unhandled exception inside `EvalRunner.run()` (typically a malformed
gold set or a metric panic).

**Fix**: Read the full stderr — the exception repr is printed. Most common
fixes:
- Gold set not a YAML list → ensure top-level is `- query: ...`.
- `gold_entities` malformed → must be list of `[str, str]`.
- `gold_edges` malformed → must be list of `[str, str, str, str]` with the
  4th element in {`EXTRACTED`, `INFERRED`, `AMBIGUOUS`}.

### 9.5 `factual_correctness` is 1.0 but answer is wrong

**Symptom**: `factual_correctness=1.0` with `details.reason="both_empty"`.

**Cause**: The LLM extracted zero facts from both the prediction and the gold
answer. The metric returns **1.0 by convention** (PRD §5.3.3) — "no facts to
disagree on".

**Fix**: Make sure `gold_answer` is non-trivial (at least one sentence with
verifiable content). If the LLM persistently extracts nothing, see
[§9.2](#92-judge-returns-empty-fact-list-every-time).

---

**Owner**: 寇豆码 (backend) · **Reviewer**: 高见远 (architect) · **Sign-off**: 齐活林 (PM)
