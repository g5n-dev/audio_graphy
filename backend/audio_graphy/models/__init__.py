"""SQLAlchemy ORM models package.

All models register themselves on ``Base.metadata`` via import side-effects.
Importing this package (or ``from audio_graphy.models import *``) ensures
all 13 tables are registered for alembic autogenerate and metadata introspection.

Table groups:
    - Global tables (Base): tenants, prompts
    - Tenant-scoped business tables (TenantScopedBase):
      users, recordings, segments, chunks, tag_facts, tag_current,
      tag_stats, vectors_entity, vectors_chunk, audit_logs, llm_call_logs

See: docs/m1.4-prd.md §5, docs/m1.4-architecture.md §2
"""

from __future__ import annotations

from audio_graphy.models.audit_log import AuditLog
from audio_graphy.models.base import Base, TenantScopedBase
from audio_graphy.models.chunk import Chunk, ChunkSegment
from audio_graphy.models.community_summary import CommunitySummary
from audio_graphy.models.edge_event import EdgeEvent
from audio_graphy.models.entity_alias import EntityAlias
from audio_graphy.models.enums import (
    PipelineState,
    RecordingStatus,
    TagSource,
    UserRole,
)
from audio_graphy.models.erasure_outbox import ErasureOutbox
from audio_graphy.models.eval_run import EvalRunORM
from audio_graphy.models.leiden_job import LeidenJob
from audio_graphy.models.llm_cache import (
    LLMCacheEntry,
    LLMCachePurge,
    LLMCacheRef,
    LLMCacheSourceGuard,
)
from audio_graphy.models.llm_call_log import LLMCallLog
from audio_graphy.models.pipeline import ProjectionOutbox, RecordingPipelineRun
from audio_graphy.models.prompt import Prompt
from audio_graphy.models.reception import (
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionAutomationRun,
    ReceptionRecording,
)
from audio_graphy.models.reception_audio import (
    ReceptionAudioArtifact,
    ReceptionAudioOperation,
    ReceptionTimelineRevision,
)
from audio_graphy.models.recompute_task import RecomputeTask
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.speaker_link import SpeakerLink
from audio_graphy.models.speaker_merge_pending import SpeakerMergePending
from audio_graphy.models.speaker_node import SpeakerNode
from audio_graphy.models.streaming_pcm_frame import StreamingPCMFrame
from audio_graphy.models.streaming_segment_receipt import StreamingSegmentReceipt
from audio_graphy.models.streaming_session import StreamingSession
from audio_graphy.models.streaming_ws_ticket import StreamingWSTicket
from audio_graphy.models.tag_current import TagCurrent
from audio_graphy.models.tag_fact import TagFact
from audio_graphy.models.tag_governance import (
    LegacyTagMapping,
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagBadcase,
    TagDeployment,
    TagDeploymentAuditSubject,
    TagDeploymentObservation,
    TagDeploymentObservationSample,
    TagEvaluationItem,
    TagEvaluationMetric,
    TagEvaluationRun,
    TagExperienceCase,
    TagExtractionJob,
    TagExtractionRun,
    TagFeedbackEvent,
    TagFeedbackLaneAssignment,
    TagGateResult,
    TaggerVersion,
    TagGoldLabel,
    TagGoldSet,
    TagGoldSetVersion,
    TagGovernanceAuditEvent,
    TagHarnessExecution,
    TagHarnessStageTrace,
    TagOptimizationRun,
    TagOptimizationTrial,
    TagReviewDecision,
    TagReviewTask,
    TagSchema,
    TagSchemaVersion,
)
from audio_graphy.models.tag_stat import TagStat
from audio_graphy.models.tenant import Tenant
from audio_graphy.models.user import User
from audio_graphy.models.vector_audio import VectorAudio
from audio_graphy.models.vector_chunk import VectorChunk
from audio_graphy.models.vector_entity import VectorEntity
from audio_graphy.models.voiceprint_vector import VoiceprintVector

__all__ = [
    "AuditLog",
    "Base",
    "Chunk",
    "ChunkSegment",
    "CommunitySummary",
    "DialogueStateTransition",
    "DialogueTagAssignment",
    "DialogueUnit",
    "EdgeEvent",
    "EntityAlias",
    "ErasureOutbox",
    "EvalRunORM",
    "LLMCacheEntry",
    "LLMCachePurge",
    "LLMCacheRef",
    "LLMCacheSourceGuard",
    "LLMCallLog",
    "LegacyTagMapping",
    "LeidenJob",
    "PipelineState",
    "ProjectionOutbox",
    "Prompt",
    "ProvenanceEvent",
    "Reception",
    "ReceptionAudioArtifact",
    "ReceptionAudioOperation",
    "ReceptionAutomationRun",
    "ReceptionRecording",
    "ReceptionTimelineRevision",
    "RecomputeTask",
    "Recording",
    "RecordingPipelineRun",
    "RecordingStatus",
    "Segment",
    "SpeakerLink",
    "SpeakerMergePending",
    "SpeakerNode",
    "StreamingPCMFrame",
    "StreamingSegmentReceipt",
    "StreamingSession",
    "StreamingWSTicket",
    "TagAssignmentCurrent",
    "TagAssignmentFact",
    "TagBadcase",
    "TagCurrent",
    "TagDeployment",
    "TagDeploymentAuditSubject",
    "TagDeploymentObservation",
    "TagDeploymentObservationSample",
    "TagEvaluationItem",
    "TagEvaluationMetric",
    "TagEvaluationRun",
    "TagExperienceCase",
    "TagExtractionJob",
    "TagExtractionRun",
    "TagFact",
    "TagFeedbackEvent",
    "TagFeedbackLaneAssignment",
    "TagGateResult",
    "TagGoldLabel",
    "TagGoldSet",
    "TagGoldSetVersion",
    "TagGovernanceAuditEvent",
    "TagHarnessExecution",
    "TagHarnessStageTrace",
    "TagOptimizationRun",
    "TagOptimizationTrial",
    "TagReviewDecision",
    "TagReviewTask",
    "TagSchema",
    "TagSchemaVersion",
    "TagSource",
    "TagStat",
    "TaggerVersion",
    "Tenant",
    "TenantScopedBase",
    "User",
    "UserRole",
    "VectorAudio",
    "VectorChunk",
    "VectorEntity",
    "VoiceprintVector",
]
