"""Tags governance service layer — three-layer tag management.

Layer 1: facts.py — TagFactsService (append-only INSERT)
Layer 2: current_view.py — TagCurrentService (upsert MAX version)
Layer 3: stats.py — TagStatsService (delta -old +new aggregation)
Orchestrator: recompute.py — RecomputeService

See: docs/m3-architecture.md §3.2.
"""

from __future__ import annotations
