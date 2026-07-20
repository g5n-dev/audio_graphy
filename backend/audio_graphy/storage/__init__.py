"""AudioGraphy storage layer — file_index, mysql_vector, graph_networkx.

Storage modules provide persistence for the core algorithm layer:
    - FileIndex: working_dir JSON KV stores + LLM response cache
    - MySQLVectorStore: brute-force cosine vector search (Phase 1)
    - NetworkXGraphStore: MultiDiGraph + GraphML persistence

All public APIs are async; file I/O is wrapped with ``asyncio.to_thread``.
"""

from __future__ import annotations
