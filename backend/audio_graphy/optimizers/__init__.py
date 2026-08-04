"""Offline prompt compilation for the semantic-tag Harness.

Nothing here runs on the serving path. A compiler turns reviewed feedback into a
prompt candidate, and the existing optimizer/evaluation/deployment machinery decides
whether that candidate is allowed anywhere near production.

Modules that depend on optional extras (DSPy, TextGrad) are imported lazily so the
API and worker images can stay free of them.
"""

from __future__ import annotations
