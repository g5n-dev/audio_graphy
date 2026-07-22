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

## GraphRAG (Microsoft, 2024)

The level-hierarchy community-summary pattern and map-reduce global
search pattern follow the GraphRAG paper
(https://github.com/microsoft/graphrag, MIT license). AudioGraphy's
`core/community_summary.py`, `core/global_search.py`, and
`core/compression.py` are original implementations inspired by the
GraphRAG design; no Microsoft source code is included.

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
