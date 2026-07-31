# Third-Party Notices

AudioGraphy incorporates concepts and design patterns from the following
open-source projects. This file lists the conceptual attributions; full
license texts for direct code dependencies live in `pyproject.toml` /
`package.json`.

## Bi-temporal edge model

Design references the Graphiti paradigm
(https://getagraphiti.com, MIT license) for the four-timestamp
`valid_at` / `invalid_at` / `created_at` / `expired_at` schema and the
dual-track supersede pattern. AudioGraphy's implementation is original
Python code; no Graphiti source is included.

## HIT-Leiden incremental community detection

The incremental + threshold-based fallback design references the
HIT-Leiden algorithm (2023). AudioGraphy uses `leidenalg` (BSD-3-Clause)
when available and falls back to `networkx.algorithms.community`
(BSD-3-Clause) for environments without `igraph`/`leidenalg`.

## VideoRAG / LightRAG / nano-graphrag (design lineage)

AudioGraphy's graph-RAG design lineage traces to the projects below.
**No source code from any of them is included in this repository**, and
AudioGraphy declares no build-time or runtime dependency on them. The
relationship is architectural: the overall pipeline shape (chunk → LLM
entity/relation extraction under a delimiter protocol → cross-chunk merge
into a graph → dual-channel naive + graph retrieval → LLM rerank).

| Project | License | Copyright |
|---------|---------|-----------|
| [HKUDS/VideoRAG](https://github.com/HKUDS/VideoRAG) | Dual license — see note below | Copyright (c) 2024 Vimo & VideoRAG Project |
| [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) | MIT | Copyright (c) 2025 LightRAG Team |
| [gusye1234/nano-graphrag](https://github.com/gusye1234/nano-graphrag) | MIT | Copyright (c) 2024 Gustavo Ye |

### Note on the VideoRAG license, and why AudioGraphy remains MIT

VideoRAG does not ship a plain MIT license. It ships a custom document,
"VIMO & VIDEORAG PROJECT DUAL LICENSE", with two parts:

- **Part 1 — Framework architecture**: the overall system architecture and
  design, framework interfaces and abstractions, core organizational
  structure and methodology, documentation and conceptual designs.
  **MIT License**, Copyright (c) 2024 Vimo & VideoRAG Project.
- **Part 2 — The implementation as shipped**: **NonCommercial use only**,
  because that code hardcodes ImageBind (CC BY-NC-SA 4.0). MiniCPM
  (Apache-2.0) is also integrated there.

**AudioGraphy uses only Part 1.** It contains no VideoRAG source code and
integrates neither ImageBind nor MiniCPM-V — the visual-modality stages
those models serve do not exist in an audio product and were never
implemented. VideoRAG's own terms cover this case ("FRAMEWORK ARCHITECTURE
ONLY — If you use only the architectural concepts, designs, and framework
structure WITHOUT the current model implementations: → MIT License
applies"), so the Part 2 NonCommercial restriction is not triggered and
AudioGraphy is distributed under its own MIT license without conflict.

### Interface conventions reproduced verbatim

Format and filename strings adopted deliberately, so that an AudioGraphy
`working_dir` stays legible to anyone familiar with the upstream tools.
These are interface conventions, not code:

- `storage/graph_networkx.py` — the GraphML filename
  `graph_chunk_entity_relation.graphml`, matching the file upstream's
  `NetworkXStorage` produces.
- `storage/file_index.py` — the JSON KV store names `kv_store_text_chunks`,
  `kv_store_llm_response_cache`, `kv_store_video_segments`,
  `kv_store_video_path`. The two `video_*` names are VideoRAG-specific and
  are kept only for layout familiarity; in AudioGraphy they hold audio
  segment and audio path data.
- `core/types.py` — the delimiter triple `<|>` / `##` / `<|COMPLETE|>`.
  These values originate from **Microsoft GraphRAG v0.1.x** (MIT,
  Copyright (c) Microsoft Corporation) and reached wider circulation via
  **nano-graphrag** (MIT, Copyright (c) 2024 Gustavo Ye). AudioGraphy
  adopts the three constants only; it does not reproduce the accompanying
  English prompt text, and its relation record layout differs (see
  "Prompts" below).

### Independently implemented in AudioGraphy

Different data structures, different algorithm steps, no shared
identifiers. AudioGraphy defines no equivalent of upstream's
`BaseKVStorage` / `BaseVectorStorage` / `BaseGraphStorage` abstractions:

- `core/chunker.py` — chunking on VAD/ASR segment boundaries (segments are
  atomic), not token-window chunking.
- `core/graph.py` — cross-chunk merge by majority-vote entity type and
  deduplicated description concatenation, not LLM description summarisation.
- `core/extractor.py` — class-based extractor with a Chinese gleaning loop
  and tuple-keyed deduplication.
- `core/entity_merger.py` — three-layer normalisation (NFKC → MySQL alias
  table → rapidfuzz WRatio).
- `core/retrieval.py` — three-channel (text / graph / audio) union retrieval.
- `core/rerank.py` — LLM-as-judge filtering and answer generation.
- `core/leiden.py` — incremental Leiden with snapshot diffing.
- `storage/` — `FileIndex`, `MySQLVectorStore`, `NetworkXGraphStore`, each a
  standalone concrete class.

### Prompts

Every prompt under `audio_graphy/prompts/` and `audio_graphy/eval/prompts/`
is original Chinese text authored for this project. They reuse only the
GraphRAG delimiter protocol (`{tuple_delimiter}` / `{record_delimiter}` /
`{completion_delimiter}`). The relation record layout differs from
upstream's and is not wire-compatible: upstream emits
`("relationship"<|>source<|>target<|>description<|>strength)` with a numeric
strength in the final field, whereas AudioGraphy emits
`("关系"<|>source<|>relation<|>target<|>detail)` with a relation label in the
third field and no numeric strength.

## GraphRAG (Microsoft, 2024)

AudioGraphy follows three patterns from Microsoft GraphRAG
(https://github.com/microsoft/graphrag, MIT license,
Copyright (c) Microsoft Corporation):

1. The entity-extraction **delimiter protocol** — `{tuple_delimiter}` /
   `{record_delimiter}` / `{completion_delimiter}`, with the default values
   `<|>` / `##` / `<|COMPLETE|>` (see `core/types.py`). AudioGraphy adopts
   the protocol and the three constants; the prompt text that accompanies
   them upstream is not reproduced — AudioGraphy's prompts are original
   Chinese text with a different relation record layout.
2. The level-hierarchy **community-summary** pattern.
3. The map-reduce **global search** pattern.

`core/extractor.py`, `core/community_summary.py`, `core/global_search.py`
and `core/compression.py` are original implementations inspired by these
designs. No Microsoft source code and no Microsoft prompt text is included.

## SpeakerLinker Layer 2 fuzzy matcher

The two-stage fuzzy match (rapidfuzz token_ratio ≥ 0.85 → AMBIGUOUS,
voiceprint cosine ≥ 0.7 → CONFIRMED) is original to AudioGraphy.
`rapidfuzz` (MIT) and `numpy` (BSD-3-Clause) are used as numerical
backends.

## Open-source dependencies

See `pyproject.toml` for the authoritative Python dependency list and
`frontend/package.json` for the frontend dependency list. Notable
licenses:

| Library | License |
|---------|---------|
| FastAPI | MIT |
| SQLAlchemy 2.x | MIT |
| Pydantic | MIT |
| APScheduler | MIT |
| NetworkX | BSD-3-Clause |
| leidenalg | BSD-3-Clause |
| rapidfuzz | MIT |
| React | MIT |
| Arco Design Web React | MIT |
| AntV G6 | MIT |
| axios | MIT |
