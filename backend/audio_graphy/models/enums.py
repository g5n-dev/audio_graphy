"""Enumeration types for AudioGraphy ORM models.

These Python ``enum.Enum`` classes provide application-layer type safety.
The database columns use ``String(N)`` + ``CheckConstraint`` (not SQL ENUM)
to avoid ALTER ENUM migration pain. See docs/m1.4-architecture.md §1.3.

Usage::

    from audio_graphy.models.enums import UserRole

    user = User(role=UserRole.ADMIN.value)
"""

from __future__ import annotations

import enum


class UserRole(enum.Enum):
    """RBAC roles for AudioGraphy users (DESIGN.md §14.1)."""

    ADMIN = "admin"
    INSPECTOR = "inspector"
    AGENT = "agent"
    VIEWER = "viewer"


class RecordingStatus(enum.Enum):
    """Lifecycle status for a recording (PRD §5.3)."""

    QUEUED = "queued"
    PROCESSING = "processing"
    INDEXED = "indexed"
    READY_NO_SPEECH = "ready_no_speech"
    FAILED = "failed"
    ARCHIVED = "archived"


class PipelineState(enum.Enum):
    """Pipeline processing stage for a recording (PRD §5.3).

    Flow: pending -> vad -> asr -> chunking -> embedding -> extraction ->
    graph_merge -> tagging -> done (or error at any stage).
    """

    PENDING = "pending"
    VAD = "vad"
    ASR = "asr"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    EXTRACTION = "extraction"
    GRAPH_MERGE = "graph_merge"
    TAGGING = "tagging"
    DONE = "done"
    ERROR = "error"


class TagSource(enum.Enum):
    """Source of a tag judgment (PRD §5.6)."""

    LLM = "llm"
    MANUAL = "manual"
