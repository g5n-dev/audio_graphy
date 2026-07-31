"""LLM gateway and its request/response contract.

Sits below ``services/`` so that ``core/`` can depend on it. The gateway is a
decorator over ``adapters.protocols.LLMAdapter`` — retry, concurrency limiting,
the multi-level cache, singleflight leases, cost accounting and observability —
and imports nothing from the application layers above it.
"""

from __future__ import annotations
