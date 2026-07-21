# AudioGraphy Eval Smoke Gold Set

This directory ships a small, realistic gold set used to smoke-test the
`python -m audio_graphy.eval` CLI and as a starting point for new eval sets.

## Files

| File | Purpose |
|------|---------|
| `smoke.yaml` | 10 example gold set covering easy/hard/entity/edge/tag/empty cases. |

## Adding new examples

1. Pick a `query` (user-facing Chinese question).
2. Write the reference `gold_answer` (single, authoritative phrasing).
3. Fill in `gold_context_ids` — chunk IDs that an ideal retrieval would surface.
4. Fill in `gold_entities`, `gold_edges`, `gold_tags` per
   `docs/m5-eval.md` schema. Empty lists are allowed (smoke.yaml case 7).
5. Optionally add `recording_id` (end-to-end audio) and `metadata`
   (`scenario`, `tenant`).

## YAML schema (minimal)

```yaml
- query: "..."
  gold_answer: "..."
  gold_context_ids: ["chunk-001"]
  gold_entities: [["实体文本", "实体类型"]]
  gold_edges: [["src", "rel", "dst", "EXTRACTED"]]
  gold_tags: [{tag_path: "接待.价格.优惠", value: "5万"}]
  recording_id: null              # optional
  metadata: {scenario: "sales", tenant: "default"}
```

## Running the smoke set

```bash
# Mock pipeline + no-judge (CI, no GPU)
python -m audio_graphy.eval --gold-set examples/eval/smoke.yaml --no-judge \
  --report-dir reports/
```

Report outputs land in `reports/eval-<run_id>.{md,json}`.
