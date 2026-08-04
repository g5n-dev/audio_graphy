/**
 * API service functions — one file per domain.
 *
 * This module provides typed functions that call the backend API
 * via the shared httpClient.
 */

import { httpClient } from "./client";
import type {
  CreatePromptCompilationRequest,
  CreatePromptCompilationResponse,
  PromptArtifact,
  PromptArtifactDiff,
  PromptArtifactListResponse,
  PromptArtifactPromoteRequest,
  PromptArtifactPromoteResponse,
  PromptArtifactStatus,
  PromptGradientDecision,
  PromptGradientListResponse,
  PromptLabReadiness,
  PromptPatchDecisionBatch,
  ExploreResponse,
  EntityDetailResponse,
  MeResponse,
  PromptListResponse,
  PipelineRunResponse,
  QueryResponse,
  RecordingCreateRequest,
  RecordingListResponse,
  RecordingResponse,
  RecordingStatus,
  RecordingStatusResponse,
  ReindexResponse,
  SegmentListResponse,
  StatsResponse,
  TagsListResponse,
  TokenResponse,
  AnalyzeTagInsightsRequest,
  AnalyzeTagInsightsResponse,
  CreateReceptionAudioOperationRequest,
  CreateTagDeploymentRequest,
  CreateTagEvaluationRequest,
  CreateTagEvaluationResponse,
  CreateTagGoldSetRequest,
  CreateTagJobRequest,
  CreateTagOptimizationRunRequest,
  CreateTagReviewBatchRequest,
  CreateTagReviewBatchResponse,
  CreateTagSchemaRequest,
  CreateTagSchemaVersionRequest,
  CreateTaggerVersionRequest,
  CorrectDialogueTagRequest,
  CorrectDialogueTagResponse,
  DecideTagReviewRequest,
  DecideTagReviewResponse,
  DeriveDialogueTagsRequest,
  DeriveDialogueTagsResponse,
  EntityId,
  DialogueEditResponse,
  MergeDialogueUnitsRequest,
  MergeReceptionRecordingsRequest,
  ProvenanceListApiResponse,
  ProvenanceObjectType,
  ReceptionAudioOperation,
  ReceptionAudioPlanRequest,
  ReceptionAudioPlanResponse,
  ReceptionAutomationRequest,
  ReceptionAutomationResponse,
  ReceptionDialogueUnit,
  ReceptionDiscoveryRequest,
  ReceptionDiscoveryResponse,
  ReceptionListRequest,
  ReceptionListResponse,
  ReceptionProposalAcceptRequest,
  ReceptionProposalAcceptResponse,
  ReceptionProvenanceChain,
  ReceptionResponseApi,
  ReceptionStateTransition,
  ReceptionTagInsightsRequest,
  ReceptionTagInsightsResponse,
  ReceptionStateInsightsRequest,
  ReceptionStateInsightsResponse,
  ReceptionTagAssignment,
  ReceptionTranscriptItem,
  ReceptionWorkspaceApiResponse,
  ReceptionWorkspaceRequest,
  ReceptionWorkspaceResponse,
  SegmentReceptionRequest,
  SplitDialogueUnitRequest,
  StreamingTicketRequest,
  StreamingTicketResponse,
  FreezeTagGoldSetRequest,
  TagAuditEventListResponse,
  TagBadcaseListResponse,
  TagDeployment,
  TagDeploymentListResponse,
  TagDeploymentObservationListResponse,
  TagEvaluationListResponse,
  TagEvolutionOverview,
  TagFactLineageResponse,
  TagGoldSet,
  TagGoldSetVersion,
  TagGoldSetListResponse,
  TagJob,
  TagJobListResponse,
  TagOptimizationCandidateComparison,
  TagOptimizationRun,
  TagOptimizationRunListResponse,
  TagReviewListResponse,
  TagReviewTask,
  TagSchema,
  TagSchemaListResponse,
  TagSchemaVersion,
  TaggerVersion,
  TaggerVersionListResponse,
} from "@/types/api";

// ============================================================
// Auth
// ============================================================

export async function login(
  email: string,
  password: string,
): Promise<TokenResponse> {
  const { data } = await httpClient.post<TokenResponse>("/auth/login", {
    email,
    password,
  });
  return data;
}

export async function getMe(): Promise<MeResponse> {
  const { data } = await httpClient.get<MeResponse>("/auth/me");
  return data;
}

// ============================================================
// Recordings
// ============================================================

export async function listRecordings(params?: {
  page?: number;
  page_size?: number;
  store_id?: string;
  status?: RecordingStatus;
  agent_name?: string;
}): Promise<RecordingListResponse> {
  const { data } = await httpClient.get<RecordingListResponse>("/recordings", {
    params,
  });
  return data;
}

export async function getRecording(id: number): Promise<RecordingResponse> {
  const { data } = await httpClient.get<RecordingResponse>(`/recordings/${id}`);
  return data;
}

/** Register a server-side audio file for pipeline processing (admin only). */
export async function createRecording(
  body: RecordingCreateRequest,
): Promise<RecordingResponse> {
  const { data } = await httpClient.post<RecordingResponse>(
    "/recordings",
    body,
  );
  return data;
}

/** Lightweight status endpoint used for progress polling. */
export async function getRecordingStatus(
  id: number,
): Promise<RecordingStatusResponse> {
  const { data } = await httpClient.get<RecordingStatusResponse>(
    `/recordings/${id}/status`,
  );
  return data;
}

/** Durable pipeline-run detail (stage checklist + failure diagnostics). */
export async function getRecordingProcessingRun(
  recordingId: number,
  runId: number,
): Promise<PipelineRunResponse> {
  const { data } = await httpClient.get<PipelineRunResponse>(
    `/recordings/${recordingId}/processing-runs/${runId}`,
  );
  return data;
}

/** Re-queue a recording for indexing (admin only; force skips hash check). */
export async function reindexRecording(
  id: number,
  body: { force?: boolean } = {},
): Promise<ReindexResponse> {
  const { data } = await httpClient.post<ReindexResponse>(
    `/recordings/${id}/reindex`,
    { force: body.force ?? false },
  );
  return data;
}

export async function getSegments(
  recordingId: number,
  params?: { page?: number; page_size?: number },
): Promise<SegmentListResponse> {
  const { data } = await httpClient.get<SegmentListResponse>(
    `/recordings/${recordingId}/segments`,
    { params },
  );
  return data;
}

// ============================================================
// Graph
// ============================================================

export async function exploreGraph(params?: {
  node_type?: string;
  min_degree?: number;
  limit?: number;
  edge_limit?: number;
}): Promise<ExploreResponse> {
  const { data } = await httpClient.get<ExploreResponse>("/graph/explore", {
    params,
  });
  return data;
}

export async function getEntity(name: string): Promise<EntityDetailResponse> {
  const { data } = await httpClient.get<EntityDetailResponse>(
    `/graph/entity/${encodeURIComponent(name)}`,
  );
  return data;
}

export async function getSubgraph(
  entity: string,
  maxHops: number = 1,
  limit: number = 50,
  edgeLimit: number = 5_000,
): Promise<ExploreResponse> {
  const { data } = await httpClient.get<ExploreResponse>("/graph/subgraph", {
    params: {
      entity,
      max_hops: maxHops,
      limit,
      edge_limit: edgeLimit,
    },
  });
  return data;
}

// ============================================================
// Tags
// ============================================================

export async function getTags(
  recordingId: number,
  view: string = "current",
): Promise<TagsListResponse> {
  const { data } = await httpClient.get<TagsListResponse>(
    `/recordings/${recordingId}/tags`,
    { params: { view } },
  );
  return data;
}

// ============================================================
// Prompts
// ============================================================

export async function listPrompts(name?: string): Promise<PromptListResponse> {
  const { data } = await httpClient.get<PromptListResponse>("/prompts", {
    params: name ? { name } : undefined,
  });
  return data;
}

// ============================================================
// Query
// ============================================================

export async function query(
  text: string,
  topK: number = 10,
): Promise<QueryResponse> {
  const { data } = await httpClient.post<QueryResponse>("/query", {
    query: text,
    top_k: topK,
  });
  return data;
}

// ============================================================
// Stats
// ============================================================

export async function getStats(params?: {
  group_by?: string;
  store_id?: string;
  tag_path?: string;
}): Promise<StatsResponse> {
  const { data } = await httpClient.get<StatsResponse>("/tags/stats", {
    params,
  });
  return data;
}

// ============================================================
// Reception dialogue workspace
// ============================================================

function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function numericField(
  value: Record<string, unknown>,
  millisecondsKey: string,
  secondsKey: string,
): number | null {
  const milliseconds = value[millisecondsKey];
  if (typeof milliseconds === "number" && Number.isFinite(milliseconds)) {
    return milliseconds;
  }
  const seconds = value[secondsKey];
  if (typeof seconds === "number" && Number.isFinite(seconds)) {
    return seconds * 1000;
  }
  return null;
}

function normalizeDialogueEvidence(
  value: unknown,
  fallbackId: string,
  fallbackRecordingId?: EntityId | null,
) {
  if (!isJsonObject(value)) return null;
  const recordingId =
    typeof value.recording_id === "string" ||
    typeof value.recording_id === "number"
      ? value.recording_id
      : fallbackRecordingId;
  if (recordingId === null || recordingId === undefined) return null;
  const textExcerpt =
    typeof value.text_excerpt === "string"
      ? value.text_excerpt
      : typeof value.text === "string"
        ? value.text
        : null;
  const kind =
    value.kind === "text" || (textExcerpt && value.kind !== "audio")
      ? "text"
      : "audio";
  const refId =
    typeof value.ref_id === "string" && value.ref_id
      ? value.ref_id
      : fallbackId;
  const sourceStartMs = numericField(
    value,
    "source_start_ms",
    "source_start_sec",
  );
  const sourceEndMs = numericField(value, "source_end_ms", "source_end_sec");
  const timelineStartMs = numericField(
    value,
    "timeline_start_ms",
    "timeline_start_sec",
  );
  const timelineEndMs = numericField(
    value,
    "timeline_end_ms",
    "timeline_end_sec",
  );
  const explicitStartMs = numericField(value, "start_ms", "start_sec");
  const explicitEndMs = numericField(value, "end_ms", "end_sec");
  const declaredSpace =
    value.coordinate_space === "source" ||
    value.coordinate_space === "timeline" ||
    value.coordinate_space === "both"
      ? value.coordinate_space
      : null;
  const coordinateSpace =
    declaredSpace ??
    (sourceStartMs !== null && timelineStartMs !== null
      ? "both"
      : timelineStartMs !== null
        ? "timeline"
        : "source");
  return {
    ref_id: refId,
    kind,
    recording_id: recordingId,
    segment_id:
      typeof value.segment_id === "string" ||
      typeof value.segment_id === "number"
        ? value.segment_id
        : null,
    coordinate_space: coordinateSpace,
    start_ms:
      explicitStartMs ??
      (coordinateSpace === "timeline" ? timelineStartMs : sourceStartMs) ??
      timelineStartMs,
    end_ms:
      explicitEndMs ??
      (coordinateSpace === "timeline" ? timelineEndMs : sourceEndMs) ??
      timelineEndMs,
    source_start_ms: sourceStartMs,
    source_end_ms: sourceEndMs,
    timeline_start_ms: timelineStartMs,
    timeline_end_ms: timelineEndMs,
    text_excerpt: textExcerpt,
  } as const;
}

function normalizeBoundaryReason(value: unknown): string {
  if (typeof value === "string") return value;
  if (isJsonObject(value)) {
    const code = typeof value.code === "string" ? value.code : null;
    const detail = typeof value.detail === "string" ? value.detail : null;
    return [code, detail].filter(Boolean).join(": ") || "unknown";
  }
  return String(value);
}

function normalizeUnit(
  unit: ReceptionWorkspaceApiResponse["dialogue_units"][number],
): ReceptionDialogueUnit {
  return {
    id: unit.id,
    unit_index: unit.unit_index,
    version: unit.version,
    start_sec: unit.start_sec,
    end_sec: unit.end_sec,
    topic: unit.topic,
    business_stage: unit.business_stage,
    summary: unit.summary,
    boundary_confidence: unit.boundary_confidence,
    boundary_reasons: unit.boundary_reasons.map(normalizeBoundaryReason),
    speaker_refs: unit.speaker_refs
      .map((speaker) => {
        if (typeof speaker === "string") return speaker;
        if (isJsonObject(speaker)) {
          const label = speaker.label ?? speaker.display_name ?? speaker.id;
          return typeof label === "string" || typeof label === "number"
            ? String(label)
            : null;
        }
        return null;
      })
      .filter((speaker): speaker is string => Boolean(speaker)),
    edit_status:
      unit.edit_status === "manual_edited" || unit.edit_status === "locked"
        ? unit.edit_status
        : "auto",
  };
}

function normalizeTranscripts(
  data: ReceptionWorkspaceApiResponse,
): ReceptionTranscriptItem[] {
  const segmentBelongsToUnit = (
    reference: unknown,
    segmentId: EntityId,
  ): boolean => {
    if (typeof reference === "string" || typeof reference === "number") {
      return String(reference) === String(segmentId);
    }
    if (!isJsonObject(reference)) return false;
    const referenceId = reference.segment_id ?? reference.id;
    return (
      (typeof referenceId === "string" || typeof referenceId === "number") &&
      String(referenceId) === String(segmentId)
    );
  };

  const inferSpeakerRole = (
    speaker: string | null,
  ): ReceptionTranscriptItem["speaker_role"] => {
    if (!speaker) return "unknown";
    const normalized = speaker.toLocaleLowerCase();
    if (
      ["agent", "sales", "advisor", "销售", "顾问", "坐席"].some((token) =>
        normalized.includes(token),
      )
    ) {
      return "agent";
    }
    if (
      ["customer", "client", "buyer", "客户", "顾客"].some((token) =>
        normalized.includes(token),
      )
    ) {
      return "customer";
    }
    return "unknown";
  };

  return data.transcript_items.map((item) => {
    const explicitUnit = data.dialogue_units.find((unit) =>
      unit.segment_refs.some((reference) =>
        segmentBelongsToUnit(reference, item.segment_id),
      ),
    );
    const midpoint =
      item.timeline_start_sec +
      (item.timeline_end_sec - item.timeline_start_sec) / 2;
    const containingUnit =
      explicitUnit ??
      data.dialogue_units.find(
        (unit) => midpoint >= unit.start_sec && midpoint <= unit.end_sec,
      );

    return {
      id: item.segment_id,
      dialogue_unit_id: containingUnit?.id ?? null,
      recording_id: item.recording_id,
      start_sec: item.timeline_start_sec,
      end_sec: item.timeline_end_sec,
      speaker_label: item.speaker?.trim() || "未知说话人",
      speaker_role: inferSpeakerRole(item.speaker),
      text: item.text.trim(),
    };
  });
}

function normalizeTags(
  data: ReceptionWorkspaceApiResponse,
): ReceptionTagAssignment[] {
  const unitsById = new Map(data.dialogue_units.map((unit) => [unit.id, unit]));
  return data.tag_assignments
    .filter((tag) => tag.is_current)
    .map((tag) => {
      const unit = unitsById.get(tag.dialogue_unit_id);
      return {
        id: tag.id,
        dialogue_unit_id: tag.dialogue_unit_id,
        group_key: tag.group_key,
        group_version: tag.group_version,
        label_key: tag.label_key,
        label_value: tag.label_value,
        confidence: tag.confidence,
        source: tag.source,
        is_manual: tag.source.toLocaleLowerCase() === "manual",
        model_run_id: tag.model_run_id,
        evidence_refs: tag.evidence_refs
          .map((evidence, index) =>
            normalizeDialogueEvidence(
              evidence,
              `tag-${tag.id}-evidence-${index}`,
              unit?.source_recording_id,
            ),
          )
          .filter((evidence): evidence is NonNullable<typeof evidence> =>
            Boolean(evidence),
          ),
      };
    });
}

function normalizeTransitions(
  data: ReceptionWorkspaceApiResponse,
): ReceptionStateTransition[] {
  return data.state_transitions.map((transition) => ({
    id: transition.id,
    sequence_no: transition.sequence_no,
    from_state: transition.from_state,
    to_state: transition.to_state,
    trigger: transition.trigger,
    confidence: transition.confidence,
    evidence_refs: transition.evidence_refs
      .map((evidence, index) =>
        normalizeDialogueEvidence(
          evidence,
          `transition-${transition.id}-evidence-${index}`,
        ),
      )
      .filter((evidence): evidence is NonNullable<typeof evidence> =>
        Boolean(evidence),
      ),
  }));
}

function normalizeReceptionWorkspace(
  data: ReceptionWorkspaceApiResponse,
): ReceptionWorkspaceResponse {
  const recordingDuration = Math.max(
    0,
    ...data.recordings.map((recording) => recording.timeline_end_sec),
  );
  const wallDuration = Math.max(
    0,
    (Date.parse(data.reception.ended_at) -
      Date.parse(data.reception.started_at)) /
      1000,
  );
  return {
    reception: {
      id: data.reception.id,
      tenant_id: data.reception.tenant_id,
      scenario: data.reception.scenario,
      store_id: data.reception.store_id,
      agent_name: data.reception.agent_name,
      agent_user_id: data.reception.agent_user_id,
      status: data.reception.status,
      merge_mode: data.reception.merge_mode,
      merge_confidence: data.reception.merge_confidence,
      started_at: data.reception.started_at,
      ended_at: data.reception.ended_at,
      duration_sec: recordingDuration || wallDuration,
      merged_audio_url: data.reception.audio_url,
      playback_expires_at: data.reception.playback_expires_at,
      version: data.reception.version,
    },
    recordings: data.recordings.map((recording) => ({
      id: recording.recording_id,
      mapping_id: recording.id,
      recording_id: recording.recording_id,
      name: `录音 #${recording.recording_id}`,
      sequence_no: recording.sequence_no,
      timeline_start_sec: recording.timeline_start_sec,
      timeline_end_sec: recording.timeline_end_sec,
      source_start_sec: recording.source_start_sec,
      source_end_sec: recording.source_end_sec,
      source_start_ms: recording.source_start_ms,
      source_end_ms: recording.source_end_ms,
      timeline_start_ms: recording.timeline_start_ms,
      timeline_end_ms: recording.timeline_end_ms,
      gap_before_ms: recording.gap_before_ms,
      time_origin_ms: recording.time_origin_ms,
      legal_source_start_ms: recording.legal_source_start_ms,
      legal_source_end_ms: recording.legal_source_end_ms,
      gap_before_sec: recording.gap_before_sec,
      audio_url: recording.audio_url,
      playback_expires_at: recording.playback_expires_at,
      decision_source: recording.decision_source,
      merge_confidence: recording.merge_confidence,
    })),
    dialogue_units: data.dialogue_units.map(normalizeUnit),
    transcript_items: normalizeTranscripts(data),
    tag_assignments: normalizeTags(data),
    state_transitions: normalizeTransitions(data),
    audit_events: data.provenance_events.map((event) => ({
      id: event.id,
      object_type: event.object_type,
      object_ref: event.object_ref,
      action: event.event_type,
      actor: event.actor,
      algorithm_version: event.algorithm_version,
      parent_refs: event.parent_refs.filter(isJsonObject),
      evidence_refs: event.evidence_refs
        .map((evidence, index) =>
          normalizeDialogueEvidence(
            evidence,
            `provenance-${event.id}-evidence-${index}`,
          ),
        )
        .filter((evidence): evidence is NonNullable<typeof evidence> =>
          Boolean(evidence),
        ),
      occurred_at: event.occurred_at,
      detail: event.payload,
    })),
    window: data.window,
    capabilities: data.capabilities,
    neighbors: data.neighbors
      ? {
          previous_dialogue_unit: data.neighbors.previous_dialogue_unit
            ? normalizeUnit(data.neighbors.previous_dialogue_unit)
            : null,
          next_dialogue_unit: data.neighbors.next_dialogue_unit
            ? normalizeUnit(data.neighbors.next_dialogue_unit)
            : null,
        }
      : undefined,
    active_audio_operation: data.active_audio_operation ?? null,
  };
}

export async function getReceptionWorkspace(
  receptionId: EntityId,
  params: ReceptionWorkspaceRequest = {},
): Promise<ReceptionWorkspaceResponse> {
  const { data } = await httpClient.get<ReceptionWorkspaceApiResponse>(
    `/receptions/${encodeURIComponent(String(receptionId))}/workspace`,
    {
      params: {
        window_start_sec: params.window_start_sec ?? 0,
        window_size_sec: params.window_size_sec ?? 600,
      },
    },
  );
  return normalizeReceptionWorkspace(data);
}

export async function listReceptions(
  params: ReceptionListRequest = {},
): Promise<ReceptionListResponse> {
  const { data } = await httpClient.get<ReceptionListResponse>("/receptions", {
    params,
  });
  return data;
}

export async function discoverReceptionProposals(
  body: ReceptionDiscoveryRequest,
): Promise<ReceptionDiscoveryResponse> {
  const { data } = await httpClient.post<ReceptionDiscoveryResponse>(
    "/receptions/proposals/discover",
    body,
  );
  return data;
}

export async function acceptReceptionProposal(
  body: ReceptionProposalAcceptRequest,
): Promise<ReceptionProposalAcceptResponse> {
  const { data } = await httpClient.post<ReceptionProposalAcceptResponse>(
    "/receptions/proposals/accept",
    body,
  );
  return data;
}

export async function runReceptionAutomation(
  receptionId: EntityId,
  body: ReceptionAutomationRequest = {},
): Promise<ReceptionAutomationResponse> {
  const { data } = await httpClient.post<ReceptionAutomationResponse>(
    `/receptions/${encodeURIComponent(String(receptionId))}/automation/run`,
    body,
  );
  return data;
}

export async function getReceptionAutomation(
  receptionId: EntityId,
): Promise<ReceptionAutomationResponse> {
  const { data } = await httpClient.get<ReceptionAutomationResponse>(
    `/receptions/${encodeURIComponent(String(receptionId))}/automation`,
  );
  return data;
}

export async function mergeReceptionRecordings(
  receptionId: EntityId,
  body: MergeReceptionRecordingsRequest,
): Promise<ReceptionResponseApi> {
  const { data } = await httpClient.post<ReceptionResponseApi>(
    `/receptions/${encodeURIComponent(String(receptionId))}/merge`,
    body,
  );
  return data;
}

export async function createReceptionAudioPlan(
  receptionId: EntityId,
  body: ReceptionAudioPlanRequest,
): Promise<ReceptionAudioPlanResponse> {
  const { data } = await httpClient.post<ReceptionAudioPlanResponse>(
    `/receptions/${encodeURIComponent(String(receptionId))}/audio-plans`,
    body,
  );
  return data;
}

export async function createReceptionAudioOperation(
  receptionId: EntityId,
  body: CreateReceptionAudioOperationRequest,
  idempotencyKey: string,
): Promise<ReceptionAudioOperation> {
  const { data } = await httpClient.post<ReceptionAudioOperation>(
    `/receptions/${encodeURIComponent(String(receptionId))}/audio-operations`,
    body,
    { headers: { "Idempotency-Key": idempotencyKey } },
  );
  return data;
}

export async function getReceptionAudioOperation(
  receptionId: EntityId,
  operationId: EntityId,
): Promise<ReceptionAudioOperation> {
  const { data } = await httpClient.get<ReceptionAudioOperation>(
    `/receptions/${encodeURIComponent(String(receptionId))}/audio-operations/${encodeURIComponent(String(operationId))}`,
  );
  return data;
}

export async function cancelReceptionAudioOperation(
  receptionId: EntityId,
  operationId: EntityId,
): Promise<ReceptionAudioOperation> {
  const { data } = await httpClient.post<ReceptionAudioOperation>(
    `/receptions/${encodeURIComponent(String(receptionId))}/audio-operations/${encodeURIComponent(String(operationId))}/cancel`,
  );
  return data;
}

export async function createStreamingTicket(
  body: StreamingTicketRequest,
): Promise<StreamingTicketResponse> {
  const { data } = await httpClient.post<StreamingTicketResponse>(
    "/ws/tickets",
    body,
  );
  return data;
}

export async function segmentReception(
  receptionId: EntityId,
  body: SegmentReceptionRequest,
): Promise<DialogueEditResponse> {
  const { data } = await httpClient.post<DialogueEditResponse>(
    `/receptions/${encodeURIComponent(String(receptionId))}/segment`,
    body,
  );
  return data;
}

export async function splitDialogueUnit(
  receptionId: EntityId,
  unitId: EntityId,
  body: SplitDialogueUnitRequest,
): Promise<DialogueEditResponse> {
  const { data } = await httpClient.post<DialogueEditResponse>(
    `/receptions/${encodeURIComponent(String(receptionId))}/dialogue-units/${encodeURIComponent(String(unitId))}/split`,
    body,
  );
  return data;
}

export async function mergeDialogueUnits(
  receptionId: EntityId,
  unitId: EntityId,
  body: MergeDialogueUnitsRequest,
): Promise<DialogueEditResponse> {
  const { data } = await httpClient.post<DialogueEditResponse>(
    `/receptions/${encodeURIComponent(String(receptionId))}/dialogue-units/${encodeURIComponent(String(unitId))}/merge`,
    body,
  );
  return data;
}

/**
 * One object's complete chronological provenance chain.
 *
 * `objectType` defaults to `reception` because that is the chain the workspace
 * asks for first, but the reason text a reviewer is forced to type when
 * correcting a tag is persisted against `dialogue_tag_assignment`, so the type
 * has to stay caller-controlled. The server 404s an object with no events at
 * all; callers branch on the status rather than expecting an empty page.
 */
export async function getReceptionProvenance(
  objectRef: EntityId,
  options: {
    objectType?: ProvenanceObjectType;
    page?: number;
    pageSize?: number;
  } = {},
): Promise<ReceptionProvenanceChain> {
  const { objectType = "reception", page, pageSize } = options;
  const { data } = await httpClient.get<ProvenanceListApiResponse>(
    `/provenance/${objectType}/${encodeURIComponent(String(objectRef))}`,
    { params: { page, page_size: pageSize } },
  );
  return {
    object_type: data.object_type,
    object_ref: data.object_ref,
    total: data.total,
    page: data.page,
    page_size: data.page_size,
    truncated: data.truncated,
    items: data.items.map((event) => ({
      id: event.id,
      object_type: event.object_type,
      object_ref: event.object_ref,
      action: event.event_type,
      actor: event.actor,
      algorithm_version: event.algorithm_version,
      parent_refs: event.parent_refs.filter(isJsonObject),
      evidence_refs: event.evidence_refs
        .map((evidence, index) =>
          normalizeDialogueEvidence(
            evidence,
            `provenance-${event.id}-evidence-${index}`,
          ),
        )
        .filter((evidence): evidence is NonNullable<typeof evidence> =>
          Boolean(evidence),
        ),
      occurred_at: event.occurred_at,
      detail: event.payload,
    })),
  };
}

export async function deriveReceptionDialogueTags(
  receptionId: EntityId,
  body: DeriveDialogueTagsRequest,
): Promise<DeriveDialogueTagsResponse> {
  const { data } = await httpClient.post<DeriveDialogueTagsResponse>(
    `/receptions/${encodeURIComponent(String(receptionId))}/dialogue-tags/derive`,
    body,
  );
  return data;
}

export async function correctReceptionDialogueTag(
  receptionId: EntityId,
  assignmentId: EntityId,
  body: CorrectDialogueTagRequest,
): Promise<CorrectDialogueTagResponse> {
  const { data } = await httpClient.patch<CorrectDialogueTagResponse>(
    `/receptions/${encodeURIComponent(String(receptionId))}/dialogue-tags/${encodeURIComponent(String(assignmentId))}`,
    body,
  );
  return data;
}

// ============================================================
// Multi-group dialogue-tag insight analysis
// ============================================================

function appendQueryValues(
  query: URLSearchParams,
  key: string,
  values: ReadonlyArray<string | number> | undefined,
): void {
  values?.forEach((value) => query.append(key, String(value)));
}

export async function getReceptionTagInsights(
  params: ReceptionTagInsightsRequest = {},
): Promise<ReceptionTagInsightsResponse> {
  const query = new URLSearchParams();
  appendQueryValues(query, "store_id", params.store_id);
  appendQueryValues(query, "agent_name", params.agent_name);
  appendQueryValues(query, "scenario", params.scenario);
  appendQueryValues(query, "reception_id", params.reception_id);
  appendQueryValues(query, "group_key", params.group_key);
  appendQueryValues(query, "group_id", params.group_id);
  if (params.started_from) query.set("started_from", params.started_from);
  if (params.started_to) query.set("started_to", params.started_to);
  if (params.page !== undefined) query.set("page", String(params.page));
  if (params.page_size !== undefined) {
    query.set("page_size", String(params.page_size));
  }
  if (params.assignment_limit !== undefined) {
    query.set("assignment_limit", String(params.assignment_limit));
  }
  if (params.matrix_limit !== undefined) {
    query.set("matrix_limit", String(params.matrix_limit));
  }
  if (params.difference_limit !== undefined) {
    query.set("difference_limit", String(params.difference_limit));
  }
  if (params.evidence_summary_limit !== undefined) {
    query.set(
      "evidence_summary_limit",
      String(params.evidence_summary_limit),
    );
  }
  if (params.merge_strategy) {
    query.set("merge_strategy", params.merge_strategy);
  }
  if (params.trend_granularity) {
    query.set("trend_granularity", params.trend_granularity);
  }
  if (params.top_n_co_occurrences !== undefined) {
    query.set("top_n_co_occurrences", String(params.top_n_co_occurrences));
  }
  const suffix = query.toString();
  const { data } = await httpClient.get<ReceptionTagInsightsResponse>(
    `/reception-tag-insights${suffix ? `?${suffix}` : ""}`,
  );
  return data;
}

export async function getReceptionStateInsights(
  params: ReceptionStateInsightsRequest = {},
): Promise<ReceptionStateInsightsResponse> {
  const search = new URLSearchParams();
  const appendMany = (key: string, values?: Array<string | number>) => {
    values?.forEach((value) => search.append(key, String(value)));
  };
  appendMany("store_id", params.store_id);
  appendMany("agent_name", params.agent_name);
  appendMany("scenario", params.scenario);
  appendMany("reception_id", params.reception_id);
  if (params.started_from) search.set("started_from", params.started_from);
  if (params.started_to) search.set("started_to", params.started_to);
  if (params.transition_limit !== undefined) {
    search.set("transition_limit", String(params.transition_limit));
  }
  const suffix = search.toString();
  const { data } = await httpClient.get<ReceptionStateInsightsResponse>(
    `/reception-state-insights${suffix ? `?${suffix}` : ""}`,
  );
  return data;
}

export async function analyzeTagInsights(
  body: AnalyzeTagInsightsRequest,
): Promise<AnalyzeTagInsightsResponse> {
  const { data } = await httpClient.post<AnalyzeTagInsightsResponse>(
    "/tag-insights/analyze",
    body,
  );
  return data;
}

// ============================================================
// Tag governance closed loop
// ============================================================

export async function listTagSchemas(): Promise<TagSchemaListResponse> {
  const { data } =
    await httpClient.get<TagSchemaListResponse>("/tag-schemas");
  return data;
}

export async function createTagSchema(
  body: CreateTagSchemaRequest,
): Promise<TagSchema> {
  const { data } = await httpClient.post<TagSchema>("/tag-schemas", body);
  return data;
}

export async function createTagSchemaVersion(
  schemaId: number,
  body: CreateTagSchemaVersionRequest,
): Promise<TagSchemaVersion> {
  const { data } = await httpClient.post<TagSchemaVersion>(
    `/tag-schemas/${schemaId}/versions`,
    body,
  );
  return data;
}

export async function publishTagSchemaVersion(
  schemaId: number,
  versionId: number,
): Promise<TagSchemaVersion> {
  const { data } = await httpClient.post<TagSchemaVersion>(
    `/tag-schemas/${schemaId}/versions/${versionId}/publish`,
  );
  return data;
}

export async function listTaggerVersions(): Promise<TaggerVersionListResponse> {
  const { data } =
    await httpClient.get<TaggerVersionListResponse>("/tagger-versions");
  return data;
}

export async function createTaggerVersion(
  body: CreateTaggerVersionRequest,
): Promise<TaggerVersion> {
  const { data } = await httpClient.post<TaggerVersion>(
    "/tagger-versions",
    body,
  );
  return data;
}

export async function getTagEvolutionOverview(): Promise<TagEvolutionOverview> {
  const { data } =
    await httpClient.get<TagEvolutionOverview>("/tag-evolution/overview");
  return data;
}

export async function listTagBadcases(params?: {
  status?: string;
  failure_stage?: string;
  tag_key?: string;
  limit?: number;
}): Promise<TagBadcaseListResponse> {
  const { data } = await httpClient.get<TagBadcaseListResponse>(
    "/tag-badcases",
    { params },
  );
  return data;
}

export async function listTagOptimizationRuns(): Promise<TagOptimizationRunListResponse> {
  const { data } =
    await httpClient.get<TagOptimizationRunListResponse>(
      "/tag-optimization-runs",
    );
  return data;
}

export async function getTagOptimizationRun(
  id: number,
): Promise<TagOptimizationRun> {
  const { data } = await httpClient.get<TagOptimizationRun>(
    `/tag-optimization-runs/${id}`,
  );
  return data;
}

export async function compareTagOptimizationTrials(
  runId: number,
  leftTrialId: number,
  rightTrialId: number,
): Promise<TagOptimizationCandidateComparison> {
  const { data } =
    await httpClient.post<TagOptimizationCandidateComparison>(
      `/tag-optimization-runs/${runId}/compare`,
      {
        left_trial_id: leftTrialId,
        right_trial_id: rightTrialId,
      },
    );
  return data;
}

export async function cancelTagOptimizationRun(
  runId: number,
): Promise<TagOptimizationRun> {
  const { data } = await httpClient.post<TagOptimizationRun>(
    `/tag-optimization-runs/${runId}/cancel`,
  );
  return data;
}

export async function createTagOptimizationRun(
  body: CreateTagOptimizationRunRequest,
): Promise<TagOptimizationRun> {
  const { data } = await httpClient.post<TagOptimizationRun>(
    "/tag-optimization-runs",
    body,
  );
  return data;
}

export async function listTagJobs(): Promise<TagJobListResponse> {
  const { data } = await httpClient.get<TagJobListResponse>("/tag-jobs");
  return data;
}

export async function createTagJob(
  body: CreateTagJobRequest,
  idempotencyKey: string,
): Promise<TagJob> {
  const { data } = await httpClient.post<TagJob>("/tag-jobs", body, {
    headers: { "Idempotency-Key": idempotencyKey },
  });
  return data;
}

export async function getTagJob(id: number): Promise<TagJob> {
  const { data } = await httpClient.get<TagJob>(`/tag-jobs/${id}`);
  return data;
}

export async function getTagFactLineage(
  id: number,
): Promise<TagFactLineageResponse> {
  const { data } = await httpClient.get<TagFactLineageResponse>(
    `/tag-facts/${id}/lineage`,
  );
  return data;
}

export async function retryTagJob(id: number): Promise<TagJob> {
  const { data } = await httpClient.post<TagJob>(`/tag-jobs/${id}/retry`);
  return data;
}

export async function cancelTagJob(id: number): Promise<TagJob> {
  const { data } = await httpClient.post<TagJob>(`/tag-jobs/${id}/cancel`);
  return data;
}

export async function listTagReviews(params?: {
  status?: string;
}): Promise<TagReviewListResponse> {
  const { data } = await httpClient.get<TagReviewListResponse>(
    "/tag-reviews",
    { params },
  );
  return data;
}

export async function createTagReviewBatch(
  body: CreateTagReviewBatchRequest,
): Promise<CreateTagReviewBatchResponse> {
  const { data } = await httpClient.post<CreateTagReviewBatchResponse>(
    "/tag-reviews/create-batch",
    body,
  );
  return data;
}

export async function claimTagReview(id: number): Promise<TagReviewTask> {
  const { data } = await httpClient.post<TagReviewTask>(
    `/tag-reviews/${id}/claim`,
  );
  return data;
}

export async function releaseTagReview(id: number): Promise<TagReviewTask> {
  const { data } = await httpClient.post<TagReviewTask>(
    `/tag-reviews/${id}/release`,
    { force: false },
  );
  return data;
}

export async function decideTagReview(
  id: number,
  body: DecideTagReviewRequest,
): Promise<DecideTagReviewResponse> {
  const { data } = await httpClient.post<DecideTagReviewResponse>(
    `/tag-reviews/${id}/decide`,
    body,
  );
  return data;
}

export async function adjudicateTagReview(
  id: number,
  body: DecideTagReviewRequest,
): Promise<DecideTagReviewResponse> {
  const { data } = await httpClient.post<DecideTagReviewResponse>(
    `/tag-reviews/${id}/adjudicate`,
    body,
  );
  return data;
}

export async function listTagGoldSets(): Promise<TagGoldSetListResponse> {
  const { data } =
    await httpClient.get<TagGoldSetListResponse>("/tag-gold-sets");
  return data;
}

export async function createTagGoldSet(
  body: CreateTagGoldSetRequest,
): Promise<TagGoldSet> {
  const { data } = await httpClient.post<TagGoldSet>("/tag-gold-sets", body);
  return data;
}

export async function freezeTagGoldSet(
  id: number,
  body: FreezeTagGoldSetRequest,
): Promise<TagGoldSetVersion> {
  const { data } = await httpClient.post<TagGoldSetVersion>(
    `/tag-gold-sets/${id}/freeze`,
    body,
  );
  return data;
}

export async function listTagEvaluations(): Promise<TagEvaluationListResponse> {
  const { data } =
    await httpClient.get<TagEvaluationListResponse>("/tag-evaluations");
  return data;
}

export async function createTagEvaluation(
  body: CreateTagEvaluationRequest,
  idempotencyKey: string,
): Promise<CreateTagEvaluationResponse> {
  const { data } = await httpClient.post<CreateTagEvaluationResponse>(
    "/tag-evaluations",
    body,
    { headers: { "Idempotency-Key": idempotencyKey } },
  );
  return data;
}

export async function listTagDeployments(): Promise<TagDeploymentListResponse> {
  const { data } =
    await httpClient.get<TagDeploymentListResponse>("/tag-deployments");
  return data;
}

export async function createTagDeployment(
  body: CreateTagDeploymentRequest,
): Promise<TagDeployment> {
  const { data } = await httpClient.post<TagDeployment>(
    "/tag-deployments",
    body,
  );
  return data;
}

export async function listTagDeploymentObservations(
  id: number,
  limit = 200,
): Promise<TagDeploymentObservationListResponse> {
  const { data } =
    await httpClient.get<TagDeploymentObservationListResponse>(
      `/tag-deployments/${id}/observations`,
      { params: { limit } },
    );
  return data;
}

export async function approveTagDeployment(
  id: number,
  revision: number,
): Promise<TagDeployment> {
  const { data } = await httpClient.post<TagDeployment>(
    `/tag-deployments/${id}/approve`,
    undefined,
    { headers: { "If-Match": String(revision) } },
  );
  return data;
}

export async function rollbackTagDeployment(
  id: number,
  reason: string,
  revision: number,
): Promise<TagDeployment> {
  const { data } = await httpClient.post<TagDeployment>(
    `/tag-deployments/${id}/rollback`,
    { reason },
    { headers: { "If-Match": String(revision) } },
  );
  return data;
}

export async function resumeTagDeployment(
  id: number,
  reason: string,
  revision: number,
): Promise<TagDeployment> {
  const { data } = await httpClient.post<TagDeployment>(
    `/tag-deployments/${id}/resume`,
    { reason },
    { headers: { "If-Match": String(revision) } },
  );
  return data;
}

export async function listTagAuditEvents(): Promise<TagAuditEventListResponse> {
  const { data } =
    await httpClient.get<TagAuditEventListResponse>("/tag-audit-events");
  return data;
}

// ============================================================
// Prompt lab — offline prompt compilation review
// ============================================================

export async function getPromptLabReadiness(): Promise<PromptLabReadiness> {
  const { data } = await httpClient.get<PromptLabReadiness>(
    "/prompt-lab/readiness",
  );
  return data;
}

export async function listPromptArtifacts(params?: {
  status?: PromptArtifactStatus;
  limit?: number;
}): Promise<PromptArtifactListResponse> {
  const { data } = await httpClient.get<PromptArtifactListResponse>(
    "/prompt-lab/artifacts",
    { params },
  );
  return data;
}

export async function getPromptArtifact(id: number): Promise<PromptArtifact> {
  const { data } = await httpClient.get<PromptArtifact>(
    `/prompt-lab/artifacts/${id}`,
  );
  return data;
}

export async function getPromptArtifactDiff(
  id: number,
): Promise<PromptArtifactDiff> {
  const { data } = await httpClient.get<PromptArtifactDiff>(
    `/prompt-lab/artifacts/${id}/diff`,
  );
  return data;
}

/** artifact_id 在类型上不可选：后端把它声明为必填查询参数。 */
export async function listPromptGradients(params: {
  artifact_id: number;
  decision?: PromptGradientDecision;
}): Promise<PromptGradientListResponse> {
  const { data } = await httpClient.get<PromptGradientListResponse>(
    "/prompt-lab/gradients",
    { params },
  );
  return data;
}

export async function createPromptCompilation(
  body: CreatePromptCompilationRequest,
): Promise<CreatePromptCompilationResponse> {
  const { data } = await httpClient.post<CreatePromptCompilationResponse>(
    "/prompt-lab/compilations",
    body,
  );
  return data;
}

export async function decidePromptPatches(
  artifactId: number,
  body: PromptPatchDecisionBatch,
): Promise<PromptArtifact> {
  const { data } = await httpClient.post<PromptArtifact>(
    `/prompt-lab/artifacts/${artifactId}/decisions`,
    body,
  );
  return data;
}

/**
 * 把审阅通过的产物晋级为 draft 候选抽取版本。幂等：重复提交解析到已有版本，
 * 不会再铸一个。候选仍要走标签治理的评估与部署门禁。
 */
export async function promotePromptArtifact(
  artifactId: number,
  body: PromptArtifactPromoteRequest,
): Promise<PromptArtifactPromoteResponse> {
  const { data } = await httpClient.post<PromptArtifactPromoteResponse>(
    `/prompt-lab/artifacts/${artifactId}/promote`,
    body,
  );
  return data;
}
