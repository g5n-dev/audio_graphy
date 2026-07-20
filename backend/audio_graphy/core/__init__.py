"""AudioGraphy core algorithm layer — chunker, extractor, graph, retrieval, rerank.

This package implements the AudioRAG pipeline:
    recording → VAD/ASR → chunking → entity extraction → graph merge
    → dual-channel retrieval → LLM rerank → answer + citations

All public APIs are async (matching adapter Protocols) and return frozen dataclasses.
"""

from __future__ import annotations
