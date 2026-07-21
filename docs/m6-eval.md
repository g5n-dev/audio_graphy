# AudioGraphy M6 Evaluation REST API Guide

> M6 promotes the M5 evaluation subsystem from CLI-only to async REST +
> real `RAGPipeline` + position de-bias. This guide covers the 4 endpoints,
> the dual entity F1 (strict + fuzzy), and the new `entity_zh_parenting`
> v1.1 prompt for the parenting-consulting scenario.

| Section | What you get |
|---|---|
| [§1 Overview](#1-overview) | what changed vs M5 |
| [§2 Quick start](#2-quick-start) | POST + poll + download |
| [§3 Endpoints](#3-endpoints) | 4 routes + schemas |
| [§4 RAGPipeline](#4-ragpipeline-real-pipeline) | real retrieval + LLM |
| [§5 Position de-bias](#5-position-de-bias) | run judge 2× averaged |
| [§6 Entity F1 dual mode](#6-entity-f1-dual-mode-strict--fuzzy) | strict + fuzzy reporting |
| [§7 Parenting prompt v1.1](#7-parenting-prompt-v11) | scenario switching |

---

## 1. Overview

M6 adds four capabilities on top of the M5 CLI:

1. **4 REST endpoints** under `/api/v1/eval/*` for create / poll / report / list.
2. **`RAGPipeline`** — replaces the M5 `NotImplementedError` stub with real
   retrieval + LLM generation via `services.QueryService`.
3. **Position de-bias** — each LLM-judge metric runs twice (original context
   order + reversed), averaged to remove positional bias.
4. **Dual entity F1** — strict (exact match) and fuzzy (rapidfuzz WRatio ≥ 0.85)
   reported side-by-side for diagnosing near-dup clustering quality.

M5's CLI (`python -m audio_graphy.eval`) is **unchanged** — REST is an
additive channel, the CLI remains for CI / smoke tests.

---

## 2. Quick start

### 2.1 Submit + poll + download (cURL)

```bash
# Submit a new run (inspector+ role).
RUN_ID=$(curl -s -X POST http://localhost:8000/api/v1/eval/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "gold_set_path": "examples/eval/smoke.yaml",
        "pipeline": "rag",
        "judge_enabled": true,
        "k": 5,
        "position_debias": true
      }' | jq -r .run_id)

echo "Run started: $RUN_ID"

# Poll until status is completed or failed.
while true; do
  STATUS=$(curl -s "http://localhost:8000/api/v1/eval/runs/$RUN_ID" \
    -H "Authorization: Bearer $TOKEN" | jq -r .status)
  echo "  status: $STATUS"
  [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
  sleep 5
done

# Download the Markdown report.
curl -s "http://localhost:8000/api/v1/eval/runs/$RUN_ID/report?format=markdown" \
  -H "Authorization: Bearer $TOKEN" \
  -o "eval_$RUN_ID.md"
```

### 2.2 List recent runs

```bash
curl "http://localhost:8000/api/v1/eval/runs?status=completed&limit=10" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 3. Endpoints

| Method · Path | Role | Returns |
|---|---|---|
| `POST /api/v1/eval/runs` | inspector+ | `202` + `{run_id, status}` |
| `GET /api/v1/eval/runs/{run_id}` | inspector+ | `200` + full state + aggregate_metrics |
| `GET /api/v1/eval/runs/{run_id}/report?format=markdown\|json` | inspector+ | `200` + file stream |
| `GET /api/v1/eval/runs?status=&limit=&offset=` | inspector+ | `200` + paginated list |

All reads are tenant-scoped: a run created by tenant-A is invisible to tenant-B.

### POST body schema

```json
{
  "gold_set_path": "examples/eval/smoke.yaml",
  "pipeline": "rag",
  "judge_enabled": true,
  "k": 5,
  "position_debias": true,
  "metadata": {"scenario": "smoke"}
}
```

- `pipeline`: `"mock"` (echo gold) or `"rag"` (real retrieval + LLM).
- `judge_enabled`: when `false`, faithfulness / answer_relevance /
  factual_correctness are skipped (recorded as 0.0 with `details.skipped=true`).
- `position_debias`: when `true`, LLM-judge metrics run twice (orig + reversed).

### State machine

```
pending ──scheduler claim──► running ──all examples done──► completed
                                │
                                │ pipeline crash (no retry)
                                ▼
                              failed
```

---

## 4. RAGPipeline (real pipeline)

`RAGPipeline` is the real M6 implementation of the `EvalPipeline` protocol.
Each `predict(gold)` call:

1. Builds a `QueryService` (lazy; reuses the injected instance).
2. Calls `QueryService.search(query=gold.query, top_k=k)`.
3. Extracts answer text + retrieved chunk_ids + citations.
4. Runs `EntityExtractor` (GraphRAG prompts) on the answer to populate
   entities + edges.
5. Returns `PredictedResult` for downstream metrics.

For testing, `RAGPipeline` accepts a pre-built `QueryService` so unit tests
can swap in a mock that returns deterministic output without spinning up
the full pipeline.

---

## 5. Position de-bias

LLM judges are sensitive to the order of retrieved context — a fact at
position 1 scores differently than the same fact at position 5. M6 removes
this bias by running each LLM-judge metric twice:

1. Original context order (as retrieved).
2. Reversed context order (line-by-line flip of `retrieved_text`).

The mean of the two scores is reported as the de-biased metric value.
`details.debiased=True`, `details.value_original`, and `details.value_reversed`
are stamped on the `MetricResult` for transparency.

Applies only to LLM-judge metrics (`faithfulness`, `answer_relevance`,
`factual_correctness`). Retrieval / entity / edge / tag metrics are pure
set operations and are unaffected by order.

**Cost**: 10 examples × 2 = 20 LLM calls (vs. 10 without de-bias). Acceptable
for nightly eval runs; disable for quick smoke tests via
`position_debias=false`.

---

## 6. Entity F1 dual mode (strict + fuzzy)

`entity_f1` is computed **twice** in every eval run:

| Mode | Threshold | Match rule | Metric name |
|---|---|---|---|
| Strict | `1.0` (exact) | `(text, type)` set equality after NFKC + lowercase | `entity_f1` |
| Fuzzy | `0.85` (default, configurable via `entity_fuzzy_threshold`) | `rapidfuzz.fuzz.WRatio >= 0.85` AND same type | `entity_f1_fuzzy` |

Both appear in `aggregate_metrics`. The gap between strict and fuzzy
indicates near-dup clustering quality:

- `entity_f1_fuzzy >> entity_f1_strict` → many near-dups not being merged.
  Action: lower `entity_fuzzy_threshold` or seed more `entity_aliases` rows.
- `entity_f1_fuzzy ≈ entity_f1_strict` → either no near-dups, or merging
  is healthy. Healthy state.

---

## 7. Parenting prompt v1.1

`prompts/entity_zh_parenting.md` ships as v1.1 of the `entity_zh_parenting`
prompt family, registered in `versions.yaml` with `scenario: parenting_consulting`.

Entity types covered: 家长 / 顾问 / 宝宝月龄 / 育儿问题 / 育儿方案 /
商品推荐 / 课程包 / 预约事件 / 育儿专家 / 育儿方法 / 行为问题.

### Switching to v1.1

The v1.0 (automotive_sales) prompt remains the default; v1.1 is opt-in.
To switch a tenant to the parenting prompt:

```bash
# Activate prompt id=3 (entity_zh_parenting v1.1) for tenant=parenting_tenant.
curl -X POST http://localhost:8000/api/v1/prompts/3/activate \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false, "trigger_recompute": false}'
```

Audit log: `action=prompt.activate`, `target=prompt:3`.

After activation, new recordings extracted via `IngestionService` pick up
the v1.1 template (via the `scenario` field in `versions.yaml`). Existing
recordings are not re-extracted automatically — schedule a `tags/recompute`
task if you need to retroactively apply the new prompt.

### A/B test (recommended)

Run two eval sets — one with v1.0 active, one with v1.1 — and compare
`entity_f1_strict` for both. The v1.1 prompt should score higher on
parenting-consulting gold sets.

---

**End of M6 Eval Guide** — for architecture details see
[`docs/m6-architecture.md §4`](./m6-architecture.md#4-eval-rest-api-设计).
