# AudioGraphy Backend

FastAPI + SQLAlchemy + MySQL backend for the AudioGraphy store-recording graph
retrieval & multi-level tagging system.

See `../docs/DESIGN.md` for the full engineering design.

## Local development

```bash
# 1. Create venv
python3.13 -m venv .venv
source .venv/bin/activate

# 2. Install dev deps
pip install -e ".[dev]"

# 3. Run tests
pytest

# 4. Or use docker-compose (see ../docker-compose.yml)
```

## Package layout

```
audio_graphy/
├── api/         # FastAPI routers (9 endpoints)
├── adapters/    # ASR/LLM/Embed/VAD protocols + mock/real impls
├── auth/        # JWT + RBAC + tenant middleware
├── core/        # Algorithm core: chunker/extractor/graph/retrieval/rerank
├── eval/        # Evaluation rubric + golden set
├── models/      # SQLAlchemy ORM (16 tables)
├── prompts/     # entity_zh prompt + versions.yaml registry
├── storage/     # mysql_state/mysql_vector/file_index/graph_networkx
└── tags/        # facts/current_view/stats/recompute (3-layer tag model)
```
