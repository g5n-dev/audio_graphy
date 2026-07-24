/**
 * API service functions — one file per domain.
 *
 * This module provides typed functions that call the backend API
 * via the shared httpClient.
 */

import { httpClient } from "./client";
import type {
  ExploreResponse,
  EntityDetailResponse,
  MeResponse,
  PromptListResponse,
  PromptResponse,
  QueryResponse,
  RecordingListResponse,
  RecordingResponse,
  SegmentListResponse,
  StatsResponse,
  TagsListResponse,
  TokenResponse,
  AnalyzeTagInsightsRequest,
  AnalyzeTagInsightsResponse,
  DeriveDialogueTagsRequest,
  DeriveDialogueTagsResponse,
  EntityId,
  DialogueEditResponse,
  MergeDialogueUnitsRequest,
  MergeReceptionRecordingsRequest,
  ProvenanceListApiResponse,
  ReceptionAuditEvent,
  ReceptionAutomationRequest,
  ReceptionAutomationResponse,
  ReceptionDialogueUnit,
  ReceptionDiscoveryRequest,
  ReceptionDiscoveryResponse,
  ReceptionListRequest,
  ReceptionListResponse,
  ReceptionProposalAcceptRequest,
  ReceptionProposalAcceptResponse,
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
  status?: string;
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
): Promise<ExploreResponse> {
  const { data } = await httpClient.get<ExploreResponse>("/graph/subgraph", {
    params: { entity, max_hops: maxHops, limit },
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

export async function getPrompt(id: number): Promise<PromptResponse> {
  const { data } = await httpClient.get<PromptResponse>(`/prompts/${id}`);
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
      version: data.reception.version,
    },
    recordings: data.recordings.map((recording) => ({
      id: recording.recording_id,
      name: `录音 #${recording.recording_id}`,
      sequence_no: recording.sequence_no,
      timeline_start_sec: recording.timeline_start_sec,
      timeline_end_sec: recording.timeline_end_sec,
      source_start_sec: recording.source_start_sec,
      source_end_sec: recording.source_end_sec,
      gap_before_sec: recording.gap_before_sec,
      audio_url: recording.audio_url,
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

export async function getReceptionProvenance(
  receptionId: EntityId,
): Promise<ReceptionAuditEvent[]> {
  const { data } = await httpClient.get<ProvenanceListApiResponse>(
    `/provenance/reception/${encodeURIComponent(String(receptionId))}`,
  );
  return data.items.map((event) => ({
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
  }));
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
