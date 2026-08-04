/**
 * API types — mirrors backend Pydantic schemas.
 *
 * All types correspond to the FastAPI response models in backend/audio_graphy/schemas/.
 */

// ============================================================
// Common
// ============================================================

export interface ErrorResponse {
  error: {
    code: string;
    message: string;
    detail: Record<string, unknown>;
  };
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

// ============================================================
// Auth
// ============================================================

export interface LoginRequest {
  email: string;
  password: string;
}

export interface UserInfo {
  id: number;
  name: string;
  email: string;
  role: string;
  tenant_id: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: UserInfo;
}

export type MeResponse = UserInfo;

// ============================================================
// Recordings
// ============================================================

export type RecordingStatus =
  | "queued"
  | "processing"
  | "indexed"
  | "ready_no_speech"
  | "failed"
  | "archived";

export interface RecordingListItem {
  id: number;
  store_id: string;
  /** Null until the agent has been resolved for the recording. */
  agent_name: string | null;
  status: RecordingStatus;
  pipeline_state: string;
  recorded_at: string | null;
  indexed_at: string | null;
  prompt_version: string | null;
  /** Durable pipeline run currently attached to this recording, if any. */
  active_pipeline_run_id: number | null;
}

/** POST /recordings request body — registers a server-side audio file. */
export interface RecordingCreateRequest {
  store_id: string;
  /** Server-side path (relative to the backend working directory / volume). */
  path: string;
  agent_name?: string;
  customer_hash?: string;
  recorded_at?: string;
  prompt_version?: string;
}

export interface TagSummary {
  tag_path: string;
  tag_value: string;
  version: number;
  prompt_version: string | null;
}

export interface RecordingResponse {
  id: number;
  tenant_id: string;
  store_id: string;
  /** Null until the agent has been resolved for the recording. */
  agent_name: string | null;
  customer_hash: string | null;
  status: RecordingStatus;
  pipeline_state: string;
  recorded_at: string | null;
  prompt_version: string | null;
  indexed_at: string | null;
  created_at: string;
  segments_count: number;
  chunks_count: number;
  current_tags: TagSummary[];
  /** Durable pipeline run currently attached to this recording, if any. */
  active_pipeline_run_id: number | null;
}

export interface RecordingListResponse {
  items: RecordingListItem[];
  total: number;
  page: number;
  page_size: number;
}

/** GET /recordings/{id}/status lightweight polling response. */
export interface RecordingStatusResponse {
  id: number;
  agent_user_id: number | null;
  status: RecordingStatus;
  pipeline_state: string;
  indexed_at: string | null;
  active_pipeline_run_id: number | null;
}

/** GET /recordings/{id}/processing-runs/{run_id} — durable pipeline run. */
export interface PipelineRunResponse {
  id: number;
  recording_id: number;
  generation: number;
  state: string;
  attempt_count: number;
  required_projections: string[];
  completed_projections: string[];
  error_code: string | null;
  error_message: string | null;
  lease_expires_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  activated_at: string | null;
}

/** POST /recordings/{id}/reindex response (202). */
export interface ReindexResponse {
  id: number;
  status: RecordingStatus;
  pipeline_state: string;
  operation_id: number;
  generation: number;
  operation_state: string;
  message: string;
}

// ============================================================
// Segments
// ============================================================

export interface SegmentResponse {
  id: number;
  idx: number;
  start_sec: number;
  end_sec: number;
  transcript: string | null;
  speaker: string | null;
  vad_conf: number | null;
}

export interface SegmentListResponse {
  recording_id: number;
  items: SegmentResponse[];
  total: number;
  page: number;
  page_size: number;
}

// ============================================================
// Graph
// ============================================================

export interface GraphNodeResponse {
  id: string;
  label: string;
  type: string;
  description: string | null;
  degree: number;
  source_ids: string[];
  recording_ids: number[];
  /** [earliest, latest] as ISO 8601, or null when no recording carries a timestamp. */
  recorded_at_range: string[] | null;
}

export interface GraphEdgeResponse {
  source: string;
  target: string;
  relation: string;
  weight: number;
  confidence: EdgeConfidence;
  confidence_score: number | null;
  source_ids: string[];
}

export interface GraphEdgeWindowResponse {
  total: number;
  returned: number;
  truncated: boolean;
  render_budget: number;
}

export interface ExploreResponse {
  nodes: GraphNodeResponse[];
  edges: GraphEdgeResponse[];
  total_nodes: number;
  total_edges: number;
  edge_window: GraphEdgeWindowResponse;
}

export interface NeighborResponse {
  id: string;
  label: string;
  type: string;
  relation: string;
  weight: number;
  confidence: string;
}

export interface EntityDetailResponse {
  node: GraphNodeResponse;
  neighbors: NeighborResponse[];
  relation_counts: Record<string, number>;
}

// ============================================================
// Tags
// ============================================================

export interface TagItem {
  tag_path: string;
  tag_value: string;
  version: number;
  prompt_version: string | null;
  source?: string;
  confidence?: number | null;
  computed_at?: string | null;
  computed_by?: number | null;
}

export interface TagsListResponse {
  recording_id: number;
  view: string;
  tags: TagItem[];
}

// ============================================================
// Prompts
// ============================================================

export interface PromptListItem {
  id: number;
  name: string;
  version: string;
  active: boolean;
  changelog: string | null;
  created_by: number;
  created_at: string;
}

export interface PromptListResponse {
  items: PromptListItem[];
}

// ============================================================
// Query
// ============================================================

/** Provenance strength of the edge a citation came from.
 *
 * Mirrors `EdgeConfidence` in backend/audio_graphy/adapters/protocols.py.
 * DEPRECATED is reachable: graph compression downgrades AMBIGUOUS edges to it.
 */
export type EdgeConfidence = "EXTRACTED" | "INFERRED" | "AMBIGUOUS" | "DEPRECATED";

export interface Citation {
  entity: string;
  chunk_id: number;
  segment_ids: number[];
  recording_id: number;
  recorded_at: string | null;
  transcript_snippet: string;
  confidence: EdgeConfidence;
}

export interface RetrievalStats {
  naive_hits: number;
  graph_hits: number;
  filtered_by_time: number;
  filtered_by_judge: number;
}

export interface QueryResponse {
  query: string;
  answer: string;
  citations: Citation[];
  retrieval_stats: RetrievalStats;
}

// ============================================================
// Stats
// ============================================================

export interface StatsItem {
  group_key: string;
  tag_count: number;
  pass_count: number;
  fail_count: number;
  pass_rate: number;
}

export interface StatsResponse {
  dimensions: string[];
  items: StatsItem[];
  total_records: number;
}

// ============================================================
// Speakers (M7 T12)
// ============================================================

export interface SpeakerListItem {
  id: number;
  tenant_id: string;
  display_name: string;
  /** Truncated voiceprint hash: vp_xxxxxxxx (PIPL-compliant). */
  voiceprint_hash: string;
  speaker_role: "agent" | "customer" | "unknown";
  recordings_count: number;
  first_seen: string | null;
  total_speech_sec: number;
  merge_confidence: number;
  merge_strategy: "voiceprint" | "fuzzy" | "manual" | "single_recording";
  ambiguity_tag: "AMBIGUOUS" | "PENDING_REVIEW" | null;
}

export interface SpeakerListResponse {
  items: SpeakerListItem[];
  total: number;
}

export interface SpeakerRecordingRef {
  recording_id: number;
  /** Null when this recording holds no voiceprint for the speaker (fuzzy/manual link). */
  voiceprint_id: string | null;
  duration_sec: number;
  strategy: string;
  ambiguity_tag: "AMBIGUOUS" | "PENDING_REVIEW" | null;
  /** Null when the link was made without a voiceprint comparison. */
  cosine_similarity: number | null;
  merge_confidence: number | null;
}

export interface SpeakerDetailResponse extends SpeakerListItem {
  recordings_list: number[];
  related_recordings: SpeakerRecordingRef[];
}

/** One diarization label in a recording, resolved to its canonical speaker. */
export interface RecordingSpeakerRef {
  /** Diarization-local label, e.g. "spk_0" — what segments actually store. */
  source_speaker_label: string;
  speaker_node_id: number;
  display_name: string;
  speaker_role: "agent" | "customer" | "unknown";
  ambiguity_tag: "AMBIGUOUS" | "PENDING_REVIEW" | null;
  merge_confidence: number | null;
  cosine_similarity: number | null;
  strategy: string;
}

export interface RecordingSpeakerListResponse {
  recording_id: number;
  items: RecordingSpeakerRef[];
}

export interface VoiceprintPolicyLayer1 {
  cosine_threshold: number;
  ambiguous_threshold: number;
}

export interface VoiceprintPolicyLayer2 {
  enabled: boolean;
  fuzzy_inferred_threshold: number;
  fuzzy_ambiguous_threshold: number;
  voiceprint_reconfirm_cosine: number;
}

export interface VoiceprintPolicySampling {
  /** "weighted_mean" or "longest_segment" (ADR-0001). */
  strategy: string;
  /** Segments shorter than this never contribute to a candidate. */
  min_segment_sec: number;
  /** Below this much qualifying speech a speaker gets no voiceprint. */
  min_total_sec: number;
  max_segments_per_speaker: number;
  /** Upstream diarization floor, distinct from the sampler's own floor. */
  diarization_min_segment_sec: number;
  max_speakers: number;
  embedding_dim: number;
}

/** GET /speakers/voiceprint-policy — read-only merge/sampling policy. */
export interface VoiceprintPolicyResponse {
  enable_voiceprint: boolean;
  adapter_voiceprint_mode: string;
  layer1: VoiceprintPolicyLayer1;
  layer2: VoiceprintPolicyLayer2;
  sampling: VoiceprintPolicySampling;
  retention_cascade: boolean;
}

// ============================================================
// Reception dialogue workspace
// ============================================================

export type EntityId = string | number;
export type ReceptionScenario = "gold" | "automotive" | "custom";
export type ReceptionMergeMode = "logical" | "physical" | "both";
export type ReceptionStatus =
  | "proposed"
  | "needs_review"
  | "confirmed"
  | "processing"
  | "ready"
  | "split"
  | "archived";
export type SpeakerRole = "agent" | "customer" | "unknown";

export interface DialogueEvidenceRef {
  ref_id: string;
  kind: "audio" | "text";
  recording_id: EntityId;
  segment_id?: EntityId | null;
  coordinate_space?: "source" | "timeline" | "both";
  start_ms: number | null;
  end_ms: number | null;
  source_start_ms?: number | null;
  source_end_ms?: number | null;
  timeline_start_ms?: number | null;
  timeline_end_ms?: number | null;
  text_excerpt?: string | null;
}

export interface ReceptionSummary {
  id: EntityId;
  tenant_id: string;
  scenario: ReceptionScenario;
  store_id: string;
  agent_name: string | null;
  agent_user_id?: number | null;
  status: string;
  merge_mode: ReceptionMergeMode;
  merge_confidence: number | null;
  started_at: string;
  ended_at: string;
  duration_sec: number;
  merged_audio_url: string | null;
  playback_expires_at: string | null;
  version: number;
}

export interface ReceptionRecordingItem {
  /**
   * Compatibility identity used by older workspace callers. It remains the
   * recording id; new editing calls must use `mapping_id` explicitly.
   */
  id: EntityId;
  /** ReceptionRecording row identity used for ordering/gap edits. */
  mapping_id?: EntityId;
  /** Immutable source Recording identity used for playback/evidence. */
  recording_id?: EntityId;
  name: string;
  sequence_no: number;
  timeline_start_sec: number;
  timeline_end_sec: number;
  source_start_sec: number;
  source_end_sec: number | null;
  source_start_ms: number;
  source_end_ms: number | null;
  timeline_start_ms: number;
  timeline_end_ms: number;
  gap_before_ms: number;
  time_origin_ms: number;
  legal_source_start_ms: number;
  legal_source_end_ms: number;
  gap_before_sec: number;
  audio_url: string | null;
  playback_expires_at: string | null;
  decision_source: "explicit" | "auto" | "manual";
  merge_confidence: number | null;
}

export interface ReceptionDialogueUnit {
  id: EntityId;
  unit_index: number;
  version: number;
  start_sec: number;
  end_sec: number;
  topic: string | null;
  business_stage: string | null;
  summary: string | null;
  boundary_confidence: number | null;
  boundary_reasons: string[];
  /** Raw speaker identities retained for the speaker track when transcript text is absent. */
  speaker_refs?: string[];
  edit_status: "auto" | "manual_edited" | "locked";
}

export interface ReceptionTranscriptItem {
  id: EntityId;
  dialogue_unit_id: EntityId | null;
  recording_id: EntityId;
  start_sec: number;
  end_sec: number;
  speaker_label: string;
  speaker_role: SpeakerRole;
  text: string;
}

export interface ReceptionTagAssignment {
  id: EntityId;
  dialogue_unit_id: EntityId;
  group_key: string;
  group_version: string;
  label_key: string;
  label_value: string;
  confidence: number | null;
  source: string;
  is_manual: boolean;
  model_run_id?: string | null;
  evidence_refs: DialogueEvidenceRef[];
}

export interface ReceptionStateTransition {
  id: EntityId;
  sequence_no: number;
  from_state: string;
  to_state: string;
  trigger: string;
  confidence: number;
  evidence_refs: DialogueEvidenceRef[];
}

export interface ReceptionAuditEvent {
  id: EntityId;
  object_type?: string;
  object_ref?: string;
  action: string;
  actor: string | null;
  algorithm_version?: string | null;
  parent_refs?: Record<string, unknown>[];
  evidence_refs?: DialogueEvidenceRef[];
  occurred_at: string;
  detail: Record<string, unknown>;
}

export interface ReceptionWorkspaceCollectionWindow {
  total: number;
  returned: number;
  limit: number;
  truncated: boolean;
}

export interface ReceptionWorkspaceWindow {
  start_sec: number;
  end_sec: number;
  size_sec: number;
  reception_duration_sec: number;
  truncated: boolean;
  has_previous: boolean;
  has_next: boolean;
  previous_start_sec: number | null;
  next_start_sec: number | null;
  total_dialogue_units: number;
  protected_dialogue_units: number;
  dialogue_units: ReceptionWorkspaceCollectionWindow;
  tag_assignments: ReceptionWorkspaceCollectionWindow;
  state_transitions: ReceptionWorkspaceCollectionWindow;
  transcript_items: ReceptionWorkspaceCollectionWindow;
  provenance_events: ReceptionWorkspaceCollectionWindow;
}

export interface ReceptionWorkspaceRequest {
  window_start_sec?: number;
  window_size_sec?: number;
}

export interface ReceptionWorkspaceCapabilities {
  can_manage_audio: boolean;
  can_run_segmentation: boolean;
  can_edit_dialogue: boolean;
  can_edit_tags: boolean;
  supports_audio_plans: boolean;
  supports_audio_operations: boolean;
  can_cancel_audio_operation: boolean;
  can_stream_audio?: boolean;
}

export interface ReceptionWorkspaceNeighbors {
  previous_dialogue_unit: ReceptionDialogueUnit | null;
  next_dialogue_unit: ReceptionDialogueUnit | null;
}

export interface ReceptionWorkspaceResponse {
  reception: ReceptionSummary;
  recordings: ReceptionRecordingItem[];
  dialogue_units: ReceptionDialogueUnit[];
  transcript_items: ReceptionTranscriptItem[];
  tag_assignments: ReceptionTagAssignment[];
  state_transitions: ReceptionStateTransition[];
  audit_events: ReceptionAuditEvent[];
  window: ReceptionWorkspaceWindow;
  /**
   * Server-owned authorization and feature capabilities. Omitted by legacy
   * servers; the client then applies the documented inspector/admin fallback.
   */
  capabilities?: Partial<ReceptionWorkspaceCapabilities>;
  /** Adjacent units outside the active 600-second page, when available. */
  neighbors?: ReceptionWorkspaceNeighbors;
  /** Server-selected non-terminal audio operation, used to resume polling after reload. */
  active_audio_operation?: ReceptionAudioOperation | null;
  /** Real normalized peaks only; omitted while the backend has not produced them. */
  waveform_peaks?: number[];
}

export interface MergeReceptionRecordingsRequest {
  recording_ids: EntityId[];
  mode: ReceptionMergeMode;
  expected_version: number;
}

export interface ReceptionAudioPlanSourceRequest {
  mapping_id: EntityId;
  gap_before_ms: number;
}

export interface ReceptionAudioPlanRequest {
  sources: ReceptionAudioPlanSourceRequest[];
  expected_version: number;
}

export interface ReceptionAudioPlanSource {
  mapping_id: EntityId;
  recording_id: EntityId;
  sequence_no: number;
  source_start_ms: number;
  source_end_ms: number;
  gap_before_ms: number;
  timeline_start_ms: number;
  timeline_end_ms: number;
}

export interface ReceptionAudioPlanResponse {
  plan_token: string;
  timeline_revision: number;
  total_duration_ms: number;
  physical_eligible: boolean;
  warnings: string[];
  sources: ReceptionAudioPlanSource[];
}

export interface CreateReceptionAudioOperationRequest {
  plan_token: string;
  mode: ReceptionMergeMode;
  expected_version: number;
}

export type ReceptionAudioOperationStatus =
  | "queued"
  | "claimed"
  | "probing"
  | "slicing"
  | "assembling"
  | "encrypting"
  | "verifying"
  | "committing"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface ReceptionAudioOperation {
  id: EntityId;
  reception_id: EntityId;
  status: ReceptionAudioOperationStatus;
  mode: ReceptionMergeMode;
  progress: number;
  error: string | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface StreamingTicketRequest {
  recording_id: EntityId;
  consent_token: string;
}

export interface StreamingTicketResponse {
  ticket: string;
  expires_at: string;
  ws_url: string;
}

export interface SegmentReceptionRequest {
  expected_version: number;
  replace_auto: boolean;
  algorithm_version: string;
}

export interface SplitDialogueUnitRequest {
  split_at_sec: number;
  expected_reception_version: number;
  expected_unit_version: number;
  reason: string;
}

export interface MergeDialogueUnitsRequest {
  other_unit_id: EntityId;
  expected_reception_version: number;
  expected_unit_version: number;
  expected_other_unit_version: number;
  reason: string;
}

export interface DialogueEditResponse {
  reception_id: EntityId;
  reception_version: number;
  dialogue_units: ReceptionDialogueUnitApiResponse[];
}

export interface CorrectDialogueTagRequest {
  expected_reception_version: number;
  expected_group_version: string;
  label_value: string;
  reason: string;
  evidence_ref_ids: string[];
}

export interface CorrectDialogueTagResponse {
  reception_id: EntityId;
  reception_version: number;
  superseded_assignment_id: EntityId;
  assignment: ReceptionTagAssignmentApiResponse;
}

/** Exact wire shape returned by backend schemas/receptions.py. */
export interface ReceptionMetadataApiResponse {
  id: number;
  tenant_id: string;
  external_session_id: string | null;
  scenario: ReceptionScenario;
  store_id: string;
  agent_name: string | null;
  agent_user_id?: number | null;
  customer_hash: string | null;
  status: ReceptionStatus;
  merge_mode: ReceptionMergeMode;
  merge_confidence: number | null;
  started_at: string;
  ended_at: string;
  /** Short-lived browser playback URL; never derive this from a server file path. */
  audio_url: string | null;
  playback_expires_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ReceptionRecordingApiResponse {
  id: number;
  recording_id: number;
  sequence_no: number;
  timeline_start_sec: number;
  timeline_end_sec: number;
  source_start_sec: number;
  source_end_sec: number | null;
  source_start_ms: number;
  source_end_ms: number | null;
  timeline_start_ms: number;
  timeline_end_ms: number;
  gap_before_ms: number;
  time_origin_ms: number;
  legal_source_start_ms: number;
  legal_source_end_ms: number;
  gap_before_sec: number;
  decision_source: "explicit" | "auto" | "manual";
  merge_confidence: number | null;
  merge_reasons: Record<string, unknown>;
  source_recorded_at: string | null;
  /** Short-lived browser playback URL supplied by the authorized audio endpoint. */
  audio_url: string;
  playback_expires_at: string;
}

export interface ReceptionTagAssignmentApiResponse {
  id: number;
  reception_id: number;
  dialogue_unit_id: number;
  group_key: string;
  group_version: string;
  label_key: string;
  label_value: string;
  confidence: number | null;
  source: string;
  priority: number;
  evidence_refs: unknown[];
  model_run_id: string | null;
  is_current: boolean;
  assigned_at: string;
}

export interface ReceptionDialogueUnitApiResponse {
  id: number;
  source_recording_id: number | null;
  unit_index: number;
  version: number;
  start_sec: number;
  end_sec: number;
  topic: string | null;
  business_stage: string | null;
  summary: string | null;
  boundary_confidence: number | null;
  boundary_reasons: unknown[];
  segment_refs: unknown[];
  speaker_refs: unknown[];
  edit_status: string;
  tag_assignments: ReceptionTagAssignmentApiResponse[];
}

export interface ReceptionStateTransitionApiResponse {
  id: number;
  dialogue_unit_id: number | null;
  sequence_no: number;
  from_state: string;
  to_state: string;
  trigger: string;
  confidence: number;
  evidence_refs: unknown[];
  algorithm_version: string;
  created_at: string;
}

export interface ReceptionTranscriptItemApiResponse {
  segment_id: number;
  recording_id: number;
  segment_index: number;
  source_start_sec: number;
  source_end_sec: number;
  timeline_start_sec: number;
  timeline_end_sec: number;
  speaker: string | null;
  text: string;
  vad_confidence: number | null;
}

export interface ReceptionWorkspaceApiResponse {
  reception: ReceptionMetadataApiResponse;
  recordings: ReceptionRecordingApiResponse[];
  dialogue_units: ReceptionDialogueUnitApiResponse[];
  state_transitions: ReceptionStateTransitionApiResponse[];
  tag_assignments: ReceptionTagAssignmentApiResponse[];
  transcript_items: ReceptionTranscriptItemApiResponse[];
  provenance_events: ProvenanceEventApiResponse[];
  window: ReceptionWorkspaceWindow;
  capabilities?: Partial<ReceptionWorkspaceCapabilities>;
  neighbors?: {
    previous_dialogue_unit: ReceptionDialogueUnitApiResponse | null;
    next_dialogue_unit: ReceptionDialogueUnitApiResponse | null;
  };
  active_audio_operation?: ReceptionAudioOperation | null;
}

export interface ReceptionResponseApi extends ReceptionMetadataApiResponse {
  recordings: ReceptionRecordingApiResponse[];
}

export interface ReceptionListRequest {
  page?: number;
  page_size?: number;
  store_id?: string;
  status?: ReceptionStatus;
  started_from?: string;
  started_to?: string;
}

export interface ReceptionListResponse {
  items: ReceptionMetadataApiResponse[];
  total: number;
  page: number;
  page_size: number;
}

export type ReceptionCandidateType =
  "merge_group" | "recording_split" | "duration_review";
export type ReceptionProposalDecision = "merge" | "reject" | "needs_review";

export interface ReceptionProposalReason {
  code: string;
  contribution: number;
  detail: string;
  hard_constraint: boolean;
}

export interface ReceptionDiscoveryRequest {
  scenario: ReceptionScenario;
  store_id: string;
  recorded_from: string;
  recorded_to: string;
  short_recording_max_sec: number;
  limit: number;
}

export interface ReceptionAutomaticProposal {
  candidate_type: ReceptionCandidateType;
  recording_ids: number[];
  decision: ReceptionProposalDecision;
  confidence: number;
  reasons: ReceptionProposalReason[];
  store_id: string;
  started_at: string;
  ended_at: string | null;
  duration_status: "available" | "unavailable";
  split_at_sec: number | null;
  at_segment_id: number | null;
  proposal_token: string | null;
  proposal_expires_at: string | null;
}

export interface ReceptionDiscoveryResponse {
  items: ReceptionAutomaticProposal[];
  total: number;
  scanned_recordings: number;
  truncated: boolean;
}

export interface ReceptionProposalAcceptRequest {
  scenario: ReceptionScenario;
  recording_ids: number[];
  external_session_id?: string;
  merge_mode: ReceptionMergeMode;
  candidate_type?: "merge_group" | "recording_split";
  split_at_sec?: number;
  at_segment_id?: number;
  proposal_token?: string;
}

export interface ReceptionSplitAcceptanceResponse {
  candidate_type: "recording_split";
  recording_id: number;
  split_at_sec: number;
  at_segment_id: number;
  source_duration_sec: number;
  receptions: [ReceptionResponseApi, ReceptionResponseApi];
  provenance_event_ids: number[];
}

export type ReceptionProposalAcceptResponse =
  ReceptionResponseApi | ReceptionSplitAcceptanceResponse;

export interface ProvenanceEventApiResponse {
  id: number;
  reception_id: number | null;
  object_type: string;
  object_ref: string;
  event_type: string;
  actor: string;
  algorithm_version: string | null;
  parent_refs: unknown[];
  evidence_refs: unknown[];
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface ProvenanceListApiResponse {
  object_type: string;
  object_ref: string;
  items: ProvenanceEventApiResponse[];
  total: number;
  page: number;
  page_size: number;
  truncated: boolean;
}

/**
 * Object kinds `GET /provenance/{object_type}/{object_ref}` answers for.
 *
 * The route validates the type against a pattern rather than an enum, but only
 * these kinds are ever written by the reception services, and a manual tag
 * correction's `reason` lands on `dialogue_tag_assignment` — never on the
 * reception itself.
 */
export type ProvenanceObjectType =
  | "reception"
  | "recording"
  | "dialogue_unit"
  | "dialogue_tag_assignment"
  | "dialogue_state_transition";

/** One page of an object's chronological provenance chain. */
export interface ReceptionProvenanceChain {
  object_type: string;
  object_ref: string;
  items: ReceptionAuditEvent[];
  total: number;
  page: number;
  page_size: number;
  /** True when this page does not contain the whole chain. */
  truncated: boolean;
}

// ============================================================
// Persisted reception dialogue tags
// ============================================================

export type DialogueTargetLabel =
  "stage" | "intent" | "objection" | "next_step" | "compliance_risk";

export interface DeriveDialogueTagsRequest {
  group_key?: string;
  group_version?: string;
  target_labels?: DialogueTargetLabel[];
  priority?: number;
  model_run_id?: string;
}

export type MissingDialogueTagReason =
  "no_verified_segment_evidence" | "missing_stage" | "no_rule_match";

export interface MissingDialogueTag {
  dialogue_unit_id: number;
  unit_index: number;
  label_key: DialogueTargetLabel;
  reason: MissingDialogueTagReason;
}

export interface DeriveDialogueTagsResponse {
  reception_id: number;
  group_key: string;
  group_version: string;
  requested_labels: DialogueTargetLabel[];
  assignment_count: number;
  superseded_count: number;
  no_op: boolean;
  assignments: ReceptionTagAssignmentApiResponse[];
  missing: MissingDialogueTag[];
}

// ============================================================
// Multi-group dialogue-tag insights
// ============================================================

export type TagMergeStrategy =
  "union" | "intersection" | "priority" | "manual_wins";
export type TrendGranularity = "day" | "week" | "month";

export interface TagInsightTimeWindow {
  start_ms: number;
  end_ms: number;
}

export interface TagInsightEvidenceRef {
  ref_id: string;
  kind: "audio" | "text";
  recording_id: string;
  start_ms?: number | null;
  end_ms?: number | null;
  text_excerpt?: string | null;
}

export interface TagInsightGroup {
  group_key: string;
  version: string;
  group_id?: string | null;
  source: string;
  priority: number;
}

export interface TagInsightAssignment {
  group_key: string;
  group_version?: string | null;
  group_id?: string | null;
  target_id: string;
  window: TagInsightTimeWindow;
  label_key: string;
  value: string;
  confidence: number | null;
  evidence_refs: TagInsightEvidenceRef[];
  is_manual: boolean;
  occurred_at: string | null;
  store_id: string | null;
  agent_id: string | null;
}

export interface AnalyzeTagInsightsRequest {
  tenant_id: string;
  merge_strategy: TagMergeStrategy;
  groups: TagInsightGroup[];
  assignments: TagInsightAssignment[];
  trend_granularity: TrendGranularity;
  top_n_co_occurrences: number;
  matrix_limit?: number;
  difference_limit?: number;
}

export interface TagInsightMatrixCell {
  group: TagInsightGroup;
  assignments: TagInsightAssignment[];
  missing: boolean;
}

export interface TagInsightMergedResult {
  strategy: TagMergeStrategy;
  values: string[];
  selected_group_keys: string[];
  confidence: number | null;
  evidence_refs: TagInsightEvidenceRef[];
}

export interface TagInsightMatrixRow {
  target_id: string;
  window: TagInsightTimeWindow;
  label_key: string;
  store_ids: string[];
  agent_ids: string[];
  cells: TagInsightMatrixCell[];
  merged: TagInsightMergedResult;
  conflict: boolean;
  missing_group_keys: string[];
}

export interface TagInsightOverview {
  group_count: number;
  assignment_count: number;
  total_cells: number;
  complete_cells: number;
  incomplete_cells: number;
  conflict_cells: number;
  conflict_rate: number;
}

export interface TagInsightCoverage {
  group_key: string;
  assigned_cells: number;
  missing_cells: number;
  coverage_rate: number;
}

export interface TagInsightPairwiseDifference {
  target_id: string;
  window: TagInsightTimeWindow;
  label_key: string;
  left_value: string;
  right_value: string;
  left_evidence_count: number;
  right_evidence_count: number;
  left_evidence_refs: TagInsightEvidenceRef[];
  right_evidence_refs: TagInsightEvidenceRef[];
}

export interface TagInsightPairwiseComparison {
  left_group_key: string;
  right_group_key: string;
  comparable_cells: number;
  agreements: number;
  differences: number;
  agreement_rate: number | null;
  left_only_cells: number;
  right_only_cells: number;
  overlap_rate: number;
  difference_items: TagInsightPairwiseDifference[];
  difference_items_truncated: boolean;
}

export interface TagInsightDistribution {
  group_key: string;
  label_key: string;
  value: string;
  count: number;
  proportion: number;
}

export interface TagInsightTrend {
  bucket_key: string;
  group_key: string;
  label_key: string;
  value: string;
  count: number;
}

export interface TagInsightCoOccurrence {
  group_key: string;
  left_label: string;
  right_label: string;
  count: number;
}

export interface TagInsightConfidence {
  group_key: string;
  bucket: string;
  count: number;
  average_confidence: number | null;
}

export interface TagInsightDimensionComparison {
  dimension: "store" | "agent";
  dimension_value: string;
  group_key: string;
  total_cells: number;
  assignment_count: number;
  missing_cells: number;
  coverage_rate: number;
  unique_targets: number;
  average_confidence: number | null;
  conflict_assignments: number;
  conflict_rate: number;
}

export interface AnalyzeTagInsightsResponse {
  tenant_id: string;
  merge_strategy: TagMergeStrategy;
  groups: TagInsightGroup[];
  truncated: boolean;
  matrix_truncated: boolean;
  difference_truncated: boolean;
  evidence_truncated: boolean;
  output_budget: {
    matrix_limit: number;
    matrix_total_rows: number;
    matrix_returned_rows: number;
    difference_limit: number;
    difference_total_items: number;
    difference_returned_items: number;
    distribution_limit: number;
    distribution_total_items: number;
    distribution_returned_items: number;
    trend_limit: number;
    trend_total_items: number;
    trend_returned_items: number;
    dimension_limit: number;
    dimension_total_items: number;
    dimension_returned_items: number;
    evidence_ref_limit: number;
    evidence_ref_count: number;
    evidence_text_byte_limit: number;
    evidence_text_bytes: number;
  };
  overview: TagInsightOverview;
  matrix: TagInsightMatrixRow[];
  coverage: TagInsightCoverage[];
  pairwise: TagInsightPairwiseComparison[];
  distributions: TagInsightDistribution[];
  trends: TagInsightTrend[];
  co_occurrences: TagInsightCoOccurrence[];
  confidence: TagInsightConfidence[];
  dimension_comparisons: TagInsightDimensionComparison[];
}

export interface ReceptionTagInsightsRequest {
  store_id?: string[];
  agent_name?: string[];
  scenario?: ReceptionScenario[];
  started_from?: string;
  started_to?: string;
  reception_id?: number[];
  group_key?: string[];
  /** Exact key@version identities. Enables historical (is_current=false) rows. */
  group_id?: string[];
  page?: number;
  page_size?: number;
  assignment_limit?: number;
  matrix_limit?: number;
  difference_limit?: number;
  evidence_summary_limit?: number;
  merge_strategy?: TagMergeStrategy;
  trend_granularity?: TrendGranularity;
  top_n_co_occurrences?: number;
}

export interface ReceptionTagEvidenceSummary {
  reception_id: number;
  dialogue_unit_id: number;
  group_id: string;
  label_key: string;
  label_value: string;
  confidence: number | null;
  evidence_count: number;
  evidence_refs: Record<string, unknown>[];
}

export interface ReceptionTagInsightsResponse {
  tenant_id: string;
  page: number;
  page_size: number;
  total_receptions: number;
  returned_reception_ids: number[];
  total_assignments: number;
  assignment_count: number;
  assignment_limit: number;
  truncated: boolean;
  assignment_truncated: boolean;
  group_truncated: boolean;
  difference_truncated: boolean;
  evidence_truncated: boolean;
  evidence_ref_limit: number;
  evidence_ref_count: number;
  evidence_summary_total: number;
  evidence_summary_count: number;
  evidence_summary_limit: number;
  evidence_summary_truncated: boolean;
  selection_mode: "current" | "exact_versions";
  selected_group_ids: string[];
  merge_strategy: TagMergeStrategy;
  trend_granularity: TrendGranularity;
  insights: AnalyzeTagInsightsResponse | null;
  evidence_summary: ReceptionTagEvidenceSummary[];
  generated_at: string;
}

// ============================================================
// Tag governance closed loop
// ============================================================

export interface TagGovernanceListResponse<T> {
  items: T[];
  total: number;
}

export type TagValueType = "enum" | "boolean" | "number" | "string";

export interface TagDefinition {
  key: string;
  name: string;
  category: string;
  value_type: TagValueType;
  allowed_values: unknown[];
  subject_types: Array<"dialogue_unit" | "reception">;
  scenarios: ReceptionScenario[];
  evidence_required: boolean;
  critical: boolean;
  critical_values?: unknown[];
  negative_values?: unknown[];
  required?: boolean;
  threshold: number;
  mutually_exclusive_with?: string[];
  depends_on?: string[];
}

export type TagSchemaVersionStatus =
  "draft" | "validated" | "published" | "deprecated";

export interface TagSchemaVersion {
  id: number;
  schema_id: number;
  version: string;
  status: TagSchemaVersionStatus;
  checksum: string;
  definitions: TagDefinition[];
  created_at: string;
  updated_at: string;
  created_by: number;
  published_by: number | null;
  published_at: string | null;
}

export interface TagSchema {
  id: number;
  tenant_id: string;
  key: string;
  name: string;
  description: string | null;
  status: "draft" | "published" | "deprecated";
  active_version_id: number | null;
  created_by: number;
  created_at: string;
  updated_at: string;
  versions?: TagSchemaVersion[];
}

export interface CreateTagSchemaRequest {
  key: string;
  name: string;
  description?: string;
}

export interface CreateTagSchemaVersionRequest {
  version: string;
  definitions: TagDefinition[];
}

export type TaggerVersionStatus =
  "draft" | "validating" | "evaluating" | "rejected" | "qualified";

export interface TaggerVersion {
  id: number;
  tenant_id: string;
  schema_version_id: number;
  version: string;
  engine: "rule" | "llm" | "hybrid";
  prompt_content: string;
  rule_bundle: Record<string, unknown>;
  model_version: string;
  thresholds: Record<string, number>;
  harness_spec_version?: string;
  harness_spec?: Record<string, unknown> | null;
  parent_version_id?: number | null;
  origin?: "manual" | "optimizer" | "bootstrap" | "migration";
  optimization_run_id?: number | null;
  change_summary?: string | null;
  config_checksum: string;
  status: TaggerVersionStatus;
  created_by: number;
  qualified_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateTaggerVersionRequest {
  schema_version_id: number;
  version: string;
  engine: "rule" | "llm" | "hybrid";
  prompt_content: string;
  rule_bundle: Record<string, unknown>;
  model_version: string;
  thresholds: Record<string, number>;
  harness_spec?: Record<string, unknown> | null;
  parent_version_id?: number | null;
  change_summary?: string | null;
}

/**
 * 与 `ck_tag_extraction_jobs_type` 保持一致（models/tag_governance.py）。
 * `prompt_compile` 由提示词实验室的编译流程写入，同样出现在 `GET /tag-jobs` 里。
 */
export type TagJobType =
  | "extract"
  | "recompute"
  | "review_batch"
  | "evaluate"
  | "optimize"
  | "remediate"
  | "prompt_compile";
export type TagJobStatus =
  | "queued"
  | "running"
  | "retry_wait"
  | "completed"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface TagJobScope {
  reception_ids?: EntityId[];
  dialogue_unit_ids?: number[];
  store_ids?: string[];
  group_ids?: string[];
  label_keys?: string[];
  [key: string]: unknown;
}

export interface CreateTagJobRequest {
  job_type: "extract" | "recompute";
  scope: TagJobScope;
}

export interface TagJob {
  id: number;
  tenant_id: string;
  job_type: TagJobType;
  status: TagJobStatus;
  scope: TagJobScope;
  tagger_version_id: number | null;
  origin?: "manual" | "serving" | "backfill" | "monitor" | "system";
  total_items: number;
  completed_items: number;
  failed_items: number;
  failed_subset?: unknown[];
  attempt_count: number;
  max_attempts: number;
  revision: number;
  lease_owner: string | null;
  lease_expires_at: string | null;
  next_attempt_at: string | null;
  last_error_code: string | null;
  last_error_message: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export type TagReviewStatus = "pending" | "claimed" | "resolved" | "skipped";
export type TagReviewDecisionAction =
  "accept" | "correct" | "reject" | "uncertain" | "escalate";
export type TagTruthState =
  "present" | "absent" | "not_applicable" | "uncertain";
export type TagTruthTier = "t0" | "t1" | "t2" | "t3";
export type TagReviewQueuePurpose =
  | "routine"
  | "active_learning"
  | "representative_audit"
  | "critical"
  | "gold"
  | "release_holdout"
  | "adjudication";
export type TagFailureStage =
  | "vad"
  | "asr"
  | "speaker"
  | "boundary"
  | "schema"
  | "tag_reasoning"
  | "evidence"
  | "fusion"
  | "insufficient_audio";

export interface TagReviewEvidenceRef {
  recording_id?: EntityId;
  segment_id?: EntityId;
  start_sec?: number;
  end_sec?: number;
  text_excerpt?: string;
  [key: string]: unknown;
}

export interface TagReviewTask {
  id: number;
  tenant_id: string;
  batch_id?: string | null;
  subject_type: "dialogue_unit" | "reception";
  subject_id: number | null;
  reception_id: number | null;
  tag_key: string;
  proposed_value: unknown;
  proposed_fact_id: number | null;
  schema_version_id: number | null;
  tagger_version_id: number | null;
  reason: string;
  status: TagReviewStatus;
  priority: number;
  claimed_by: number | null;
  claimed_at: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
  created_by: number | null;
  confidence: number | null;
  evidence_refs: TagReviewEvidenceRef[];
  queue_purpose?: TagReviewQueuePurpose;
  blind_mode?: boolean;
  truth_tier?: TagTruthTier;
  review_bundle_id?: string | null;
  allowed_values?: unknown[];
  selection_policy?: string | null;
  selection_policy_version?: string | null;
  sampling_probability?: number | null;
  reviewer_round?: number | null;
  requires_adjudication?: boolean;
  source_deployment_id?: number | null;
  source_extraction_run_id?: number | null;
  source_harness_execution_id?: number | null;
  sampled_deployment_stage?: TagDeploymentStatus | null;
  sampled_deployment_revision?: number | null;
  sampling_manifest_checksum?: string | null;
}

export interface CreateTagReviewSubject {
  subject_type: "dialogue_unit" | "reception";
  subject_id: number;
  reception_id?: number;
  tag_key: string;
  proposed_value?: unknown;
  proposed_fact_id?: number;
  schema_version_id?: number;
  tagger_version_id?: number;
  confidence?: number;
  evidence_refs?: TagReviewEvidenceRef[];
  priority?: number;
}

export interface CreateTagReviewBatchRequest {
  reason:
    | "conflict"
    | "missing"
    | "low_confidence"
    | "critical"
    | "random"
    | "drift"
    | "audit"
    | "gold"
    | "adjudication"
    | "active_learning";
  subjects: CreateTagReviewSubject[];
  review_bundle_id?: string;
}

export interface CreateTagReviewBatchResponse {
  batch_id?: string;
  created_count: number;
  items: TagReviewTask[];
}

export interface DecideTagReviewRequest {
  action: TagReviewDecisionAction;
  /** Required by the structured review workbench; optional for legacy callers. */
  truth_state?: TagTruthState;
  corrected_value?: unknown;
  reason_code: string;
  reason_codes?: string[];
  primary_failure_stage?: TagFailureStage;
  reviewer_confidence?: number;
  review_duration_ms?: number;
  note?: string;
  evidence_refs: TagReviewEvidenceRef[];
}

export interface TagAssignmentFact {
  id: number;
  source: string;
  tag_key: string;
  tag_value: unknown;
  input_hash?: string;
  schema_version_id?: number | null;
  tagger_version_id?: number | null;
  extraction_run_id?: number | null;
  deployment_id?: number | null;
  evidence_refs?: TagReviewEvidenceRef[];
  [key: string]: unknown;
}

export interface TagFactLineageResponse {
  fact: TagAssignmentFact;
  is_current: boolean;
  schema_version: TagSchemaVersion | null;
  tagger_version: TaggerVersion | null;
  model_version: string | null;
  extraction_run: Record<string, unknown> | null;
  job: TagJob | null;
  deployment: TagDeployment | null;
}

export interface DecideTagReviewResponse {
  task: TagReviewTask;
  decision: TagReviewDecision;
  fact: TagAssignmentFact | null;
}

export interface TagReviewDecision {
  id: number;
  tenant_id: string;
  task_id: number;
  action: TagReviewDecisionAction;
  truth_state?: TagTruthState;
  corrected_value: unknown;
  reason_code: string;
  reason_codes?: string[];
  primary_failure_stage?: TagFailureStage | null;
  reviewer_confidence?: number | null;
  note: string | null;
  evidence_refs: TagReviewEvidenceRef[];
  resulting_fact_id: number | null;
  reviewer_user_id: number;
  adjudication: boolean;
  decided_at: string;
  created_at: string;
  updated_at: string;
}

export interface TagGoldSet {
  id: number;
  tenant_id: string;
  key: string;
  name: string;
  description: string | null;
  schema_version_id: number;
  created_by: number;
  created_at: string;
  updated_at: string;
}

export interface CreateTagGoldSetRequest {
  key: string;
  name: string;
  description?: string;
  schema_version_id: number;
}

export interface TagGoldSetVersion {
  id: number;
  tenant_id: string;
  gold_set_id: number;
  version: string;
  status: "draft" | "frozen" | "retired";
  checksum: string | null;
  item_count: number;
  frozen_by: number | null;
  frozen_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface FreezeTagGoldSetRequest {
  version: string;
  cohort: {
    review_bundle_ids: string[];
    truth_tiers: Array<"t2" | "t3">;
    subject_types: Array<"dialogue_unit" | "reception">;
  };
  completeness_checklist: {
    full_applicable_matrix: true;
    frozen_input_snapshots: true;
    reception_level_isolation: true;
    t2_t3_truth_only: true;
  };
}

export interface TagQualityMetrics {
  macro_f1?: number;
  critical_recall?: number;
  evidence_coverage?: number;
  error_rate?: number;
  precision?: number;
  recall?: number;
  /** 评估通道：challenge 是公开验证；holdout 只有发布服务能查询。 */
  evaluation_lane?: "challenge" | "holdout";
  /** 为 true 时评估由发布服务在密封 holdout 上双跑，是创建部署的硬前提。 */
  sealed_release?: boolean;
  [key: string]: number | string | boolean | undefined;
}

export interface TagQualityGate {
  code: string;
  passed: boolean;
  actual: number | null;
  threshold: number | null;
  message: string;
}

export interface TagEvaluation {
  id: number;
  tenant_id: string;
  tagger_version_id: number;
  baseline_tagger_version_id: number;
  gold_set_version_id: number;
  status?: "queued" | "running" | "completed" | "failed";
  passed: boolean;
  metrics: TagQualityMetrics;
  baseline_metrics: TagQualityMetrics;
  supported_label_f1?: Record<string, number>;
  baseline_label_f1?: Record<string, number>;
  gates: TagQualityGate[];
  started_at: string;
  finished_at: string | null;
  created_by: number;
  created_at: string;
  updated_at: string;
}

export interface CreateTagEvaluationRequest {
  tagger_version_id: number;
  gold_set_version_id: number;
  baseline_tagger_version_id: number;
}

export interface CreateTagEvaluationResponse {
  job_id: number;
  evaluation: TagEvaluation;
}

export type TagDeploymentStatus =
  | "shadow"
  | "canary_5"
  | "canary_25"
  | "awaiting_admin"
  | "production"
  | "rolled_back"
  | "retired";

export interface TagDeployment {
  id: number;
  tenant_id: string;
  tagger_version_id: number;
  evaluation_run_id: number;
  baseline_tagger_version_id: number;
  status: TagDeploymentStatus;
  traffic_percent: number;
  revision: number;
  promotion_paused: boolean;
  pause_reason: string | null;
  created_by: number;
  approved_by: number | null;
  approved_at: string | null;
  rolled_back_by: number | null;
  rolled_back_at: string | null;
  rollback_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateTagDeploymentRequest {
  tagger_version_id: number;
  evaluation_run_id: number;
  baseline_tagger_version_id: number;
}

export interface TagDeploymentObservation {
  id: number;
  tenant_id: string;
  deployment_id: number;
  deployment_revision?: number;
  stage: TagDeploymentStatus;
  window_start: string;
  window_end: string;
  sample_count: number;
  metrics: {
    [key: string]: unknown;
    error_rate?: number;
    critical_recall?: number;
    evidence_coverage?: number;
    drift_max_jsd?: number;
    drift_paired_sample_count?: number;
    drift_min_paired_samples?: number;
    drift_jsd_threshold?: number;
    drift_eligible_tag_count?: number;
    drift_affected_tags?: string[];
    drift_by_tag?: Record<
      string,
      {
        jsd?: number;
        sample_count?: number;
        eligible?: boolean;
        breached?: boolean;
        baseline_distribution?: Record<string, number>;
        candidate_distribution?: Record<string, number>;
      }
    >;
  };
  breach_codes: string[];
  action: "observe" | "pause" | "rollback";
  /** Present only on hosted demonstration observations. */
  is_demo?: boolean;
  data_source?: "demo" | "production";
  created_at: string;
  updated_at: string;
}

export interface TagAuditEvent {
  id: number;
  tenant_id: string;
  action: string;
  actor_user_id: number | null;
  resource_type: string;
  resource_id: string;
  payload: Record<string, unknown>;
  occurred_at: string;
  created_at: string;
  updated_at: string;
}

export type TagEvolutionDriftStatus = "stable" | "watch" | "paused";
export type TagOptimizationObjectivePolicy =
  "balanced" | "quality_first" | "efficiency_guarded";
export type TagOptimizationRunStatus =
  "queued" | "running" | "completed" | "failed" | "cancelled";

export interface TagEvolutionHarnessSummary {
  id: number;
  version: string;
  status: string;
  updated_at?: string | null;
}

export interface TagEvolutionOverview {
  production_harness: TagEvolutionHarnessSummary | null;
  recommended_gold_set_version_id: number | null;
  recommended_gold_set_label?: string | null;
  quality: {
    unbiased_macro_f1?: number | null;
    critical_recall_lcb?: number | null;
    evidence_iou?: number | null;
    worst_slice_f1?: number | null;
    delta_vs_baseline?: number | null;
  };
  feedback: {
    eligible_count: number;
    new_since_last_run: number;
    representative_audit_count: number;
    adjudicated_count: number;
    coverage_rate?: number | null;
    next_run_eligible: boolean;
    blockers: string[];
  };
  drift: {
    status: TagEvolutionDriftStatus;
    input_psi?: number | null;
    output_jsd?: number | null;
    affected_slices?: string[];
  };
  release: {
    stage: TagDeploymentStatus | "offline";
    served_count: number;
    paired_count: number;
    audited_count: number;
    adjudicated_count: number;
    waiting_reasons: string[];
    promotion_paused: boolean;
  } | null;
}

export type TagBadcaseStatus =
  "open" | "candidate_fix" | "verified" | "resolved" | "reopened" | "ignored";

export interface TagBadcase {
  id: number;
  subject_type?: "dialogue_unit" | "reception";
  subject_id?: number;
  tag_key: string;
  failure_stage: TagFailureStage;
  failure_mode: string;
  cluster_key?: string | null;
  root_cause?: Record<string, unknown>;
  status: TagBadcaseStatus;
  occurrence_count: number;
  regression_result?: Record<string, unknown>;
  fix_candidate_tagger_version_id?: number | null;
  first_seen_at?: string;
  last_seen_at?: string;
  resolved_at?: string | null;
  /** Optional presentation fields returned by aggregated deployments. */
  cluster_label?: string;
  support_count?: number;
  affected_slices?: string[];
  representative_excerpt?: string | null;
  last_regression_result?: "pending" | "passed" | "failed" | null;
  updated_at?: string | null;
}

export interface TagOptimizationSourceCohort {
  source: "eligible_feedback" | "tag_insights" | "scheduled" | string;
  filters?: {
    store_ids?: string[];
    agent_names?: string[];
    reception_ids?: EntityId[];
    scenarios?: ReceptionScenario[];
    group_keys?: string[];
    started_from?: string;
    started_to?: string;
    label_keys?: string[];
    [key: string]: unknown;
  };
  group_ids?: string[];
  conflict_only?: boolean;
  [key: string]: unknown;
}

export interface CreateTagOptimizationRunRequest {
  cohort: TagOptimizationSourceCohort;
  target_policy: {
    policy: TagOptimizationObjectivePolicy;
    [key: string]: unknown;
  };
  search_budget: {
    max_trials: 8 | 16 | 24 | 32;
    sealed_holdout_queries: 1;
  };
}

export interface TagHarnessDimensionDiff {
  dimension:
    | "context"
    | "tools"
    | "generation"
    | "orchestration"
    | "memory"
    | "output"
    | string;
  before: unknown;
  after: unknown;
}

export interface TagOptimizationCandidateComparison {
  dimensions: TagHarnessDimensionDiff[];
  metric_deltas: {
    macro_f1?: number | null;
    critical_recall_lcb?: number | null;
    evidence_iou?: number | null;
    review_rate?: number | null;
    p95_latency_ms?: number | null;
    cost_per_1k?: number | null;
    [key: string]: number | null | undefined;
  };
  reward_deltas?: {
    quality_delta?: number | null;
    review_rate_delta?: number | null;
    p95_latency_delta?: number | null;
    cost_delta?: number | null;
    [key: string]: number | null | undefined;
  };
  improved_badcase_count: number;
  regressed_badcase_count: number;
  /**
   * Present on POST /tag-optimization-runs/{id}/compare (build_candidate_comparison).
   * `trial_id` is null when neither trial carries a complete reward vector yet,
   * in which case `basis` is "insufficient_completed_reward".
   */
  recommendation?: {
    trial_id: number | null;
    basis: string;
  };
  status?: "success" | "warning";
  summary?: string;
  next_actions?: string[];
  artifacts?: string[];
}

/**
 * Shape of `TagOptimizationTrial.to_dict()` as returned inside
 * GET /tag-optimization-runs/{id}. Only the columns the UI reads are listed.
 */
export interface TagOptimizationTrial {
  id: number;
  optimization_run_id: number;
  parent_trial_id?: number | null;
  candidate_tagger_version_id?: number | null;
  ordinal: number;
  status:
    | "pending"
    | "running"
    | "pruned"
    | "completed"
    | "failed"
    | "cancelled";
  phase?: "train" | "validation" | "challenge" | "holdout";
  /** Written by the optimizer as `{description, dimension}`. */
  mutation?: {
    description?: string | null;
    dimension?: string | null;
    [key: string]: unknown;
  };
  harness_spec?: Record<string, unknown>;
  reward_vector?: Record<string, number | boolean | null>;
  metrics?: Record<string, number | null>;
  gate_results?: Record<string, unknown>;
  summary?: Record<string, unknown>;
}

export interface TagOptimizationRun {
  id: number;
  job_id?: number | null;
  status: TagOptimizationRunStatus;
  phase:
    "prepare" | "search" | "validation" | "challenge" | "holdout" | "completed";
  baseline_tagger_version_id: number;
  baseline_version?: string | null;
  candidate_tagger_version_id?: number | null;
  winner_tagger_version_id?: number | null;
  candidate_version?: string | null;
  gold_set_version_id: number;
  cohort: TagOptimizationSourceCohort;
  objective: {
    policy: TagOptimizationObjectivePolicy;
    [key: string]: unknown;
  };
  search_budget: {
    max_trials: number;
    sealed_holdout_queries: number;
  };
  trigger: "manual" | "scheduled" | "feedback_threshold" | "insight";
  summary: Record<string, unknown>;
  next_actions?: unknown[];
  artifacts?: unknown[];
  completed_trials?: number;
  total_trials?: number;
  trials?: TagOptimizationTrial[];
  candidate_comparison?: TagOptimizationCandidateComparison | null;
  failure_reason?: string | null;
  is_demo?: boolean;
  data_source?: "demo" | "production";
  created_at: string;
  updated_at: string;
}

export type TagSchemaListResponse = TagGovernanceListResponse<TagSchema>;
export type TaggerVersionListResponse =
  TagGovernanceListResponse<TaggerVersion>;
export type TagJobListResponse = TagGovernanceListResponse<TagJob>;
export type TagReviewListResponse = TagGovernanceListResponse<TagReviewTask>;
export type TagGoldSetListResponse = TagGovernanceListResponse<TagGoldSet>;
export type TagEvaluationListResponse =
  TagGovernanceListResponse<TagEvaluation>;
export type TagDeploymentListResponse =
  TagGovernanceListResponse<TagDeployment>;
export type TagDeploymentObservationListResponse =
  TagGovernanceListResponse<TagDeploymentObservation>;
export type TagAuditEventListResponse =
  TagGovernanceListResponse<TagAuditEvent>;
export type TagBadcaseListResponse = TagGovernanceListResponse<TagBadcase>;
export type TagOptimizationRunListResponse =
  TagGovernanceListResponse<TagOptimizationRun>;

// ============================================================
// Cross-reception dialogue-state insights
// ============================================================

export interface ReceptionStateStageInsight {
  state: string;
  count: number;
  reception_count: number;
  incoming_count: number;
  outgoing_count: number;
  average_confidence: number | null;
}

export interface ReceptionStateTriggerInsight {
  trigger: string;
  count: number;
}

export interface ReceptionStateTransitionInsight {
  from_state: string;
  to_state: string;
  count: number;
  average_confidence: number | null;
  evidence_count: number;
  top_triggers: ReceptionStateTriggerInsight[];
  sample_reception_ids: number[];
}

export interface ReceptionStateInsightsRequest {
  store_id?: string[];
  agent_name?: string[];
  scenario?: ReceptionScenario[];
  started_from?: string;
  started_to?: string;
  reception_id?: number[];
  transition_limit?: number;
}

export interface ReceptionStateInsightsResponse {
  tenant_id?: string;
  total_receptions: number;
  total_transitions: number;
  returned_stages?: number;
  stage_limit?: number;
  returned_transitions: number;
  transition_limit: number;
  truncated: boolean;
  stages: ReceptionStateStageInsight[];
  transitions: ReceptionStateTransitionInsight[];
  generated_at: string;
}

// ============================================================
// Resumable reception automation
// ============================================================

export type ReceptionAutomationStatus =
  "pending" | "running" | "failed" | "ready";
export type ReceptionAutomationStage =
  "merge" | "segmentation" | "tagging" | "ready";

export interface ReceptionAutomationRequest {
  segmentation_algorithm?: string;
  tag_group_key?: string;
  tag_group_version?: string;
  target_labels?: DialogueTargetLabel[];
  tag_priority?: number;
}

export interface ReceptionAutomationResponse {
  id: number;
  reception_id: number;
  status: ReceptionAutomationStatus;
  stage: ReceptionAutomationStage;
  attempt_count: number;
  checkpoints: Record<string, unknown>;
  segmentation_algorithm: string;
  tag_group_key: string;
  tag_group_version: string;
  target_labels: DialogueTargetLabel[];
  tag_priority: number;
  last_error_code: string | null;
  last_error_message: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

// ============================================================
// Prompt lab — offline prompt compilation
// ============================================================

export type PromptCompilerName =
  | "builtin"
  | "builtin_grounded"
  | "dspy_mipro"
  | "dspy_bootstrap"
  | "dspy_gepa"
  | "textgrad_tgd";

export type PromptEfficiencyPolicy = "token_reduction_v1" | "quality_uplift_v1";

/**
 * verbatim 存在于后端模型层供调试，但 API 既不接受也不返回——见后端
 * schemas/prompt_lab.py 的模块注释。前端类型上就堵死。
 */
export type PromptRedactionMode = "masked" | "synthetic";

export type PromptArtifactStatus =
  | "draft"
  | "review"
  | "accepted"
  | "rejected"
  | "superseded";

export type PromptPatchKind =
  | "instruction_rewrite"
  | "constraint_add"
  | "rule_clarification";

export type PromptPatchOrigin = PromptCompilerName | "manual";

export type PromptGradientDecision = "pending" | "accepted" | "rejected";

export interface PromptLabDomainCoverage {
  /** 形如 "{subject_type}:{tag_key}"。 */
  domain: string;
  gold_count: number;
  silver_count: number;
  /** 只统计人工金标；银标可见但不计入门槛。 */
  feedback_count: number;
  meets_threshold: boolean;
}

export interface PromptLabReadiness {
  tenant_id: string;
  ready: boolean;
  gold_label_total: number;
  silver_label_total: number;
  feedback_total: number;
  feedback_threshold: number;
  domain_threshold: number;
  frozen_gold_set_versions: number;
  pending_artifacts: number;
  annotation_hours_remaining: number;
  domains: PromptLabDomainCoverage[];
  /**
   * 机器码：reviewed_feedback_below_200 / domain_support_below_30:{domain}
   * / no_reviewed_domains / no_frozen_gold_set。未知码要原样展示，不能静默丢弃。
   */
  blockers: string[];
}

export interface PromptPatch {
  patch_id: string;
  kind: PromptPatchKind;
  origin: PromptPatchOrigin;
  ordinal: number;
  body: string;
  rationale: string;
  target_tag_keys: string[];
  gradient_text: string | null;
  source_badcase_ids: number[];
  source_gold_label_ids: number[];
}

export interface PromptDemo {
  demo_id: string;
  gold_label_id: number;
  subject_type: string;
  subject_id: number;
  rendered_text: string;
  redaction_mode: PromptRedactionMode;
  source_checksum: string;
  reception_id: number | null;
  segment_ids: number[];
  recording_ids: number[];
}

export interface PromptInputBudgetReport {
  prompt_tokens: number;
  schema_tokens: number;
  fixed_tokens: number;
  usable_tokens: number;
  headroom_tokens: number;
  baseline_fixed_tokens: number;
  baseline_headroom_tokens: number;
  headroom_delta: number;
  headroom_shrink_ratio: number;
  fits: boolean;
}

export interface PromptRedactionReport {
  demo_count: number;
  by_redaction_mode: Partial<Record<PromptRedactionMode, number>>;
}

/** 列表接口省略 Prompt 正文与 patches/demos，只有详情与 diff 才带。 */
export interface PromptArtifactSummary {
  id: number;
  compilation_id: number;
  optimization_run_id: number | null;
  baseline_tagger_version_id: number;
  gold_set_version_id: number | null;
  parent_artifact_id: number | null;
  candidate_tagger_version_id: number | null;
  compiler: PromptPatchOrigin;
  compiler_version: string;
  metric_version: string;
  status: PromptArtifactStatus;
  prompt_token_estimate: number;
  accepted_patch_ids: string[];
  input_budget_report: PromptInputBudgetReport;
  redaction_report: PromptRedactionReport;
  artifact_checksum: string;
  created_at: string;
}

export interface PromptArtifact extends PromptArtifactSummary {
  baseline_prompt: string;
  rendered_prompt: string;
  patches: PromptPatch[];
  demos: PromptDemo[];
}

export interface PromptArtifactDiff {
  artifact_id: number;
  status: PromptArtifactStatus;
  baseline_prompt: string;
  candidate_prompt: string;
  patches: PromptPatch[];
  demos: PromptDemo[];
  accepted_patch_ids: string[];
  /** 候选 Prompt 的策略正文估算，不含系统包装与 schema——不可与固定开销相减。 */
  prompt_token_estimate: number;
  /**
   * 候选与基线的单次固定开销之差（同口径：均含系统包装与 schema）。
   *
   * 正数表示候选更贵。预算未测量时为 null，而不是 0——「没测过」与「没变化」不是
   * 一回事，后者才配显示成绿色。
   */
  fixed_token_delta: number | null;
  input_budget_report: PromptInputBudgetReport;
  redaction_report: PromptRedactionReport;
}

export interface PromptGradient {
  id: number;
  artifact_id: number;
  patch_id: string;
  iteration: number;
  source_badcase_id: number | null;
  tag_key: string | null;
  failure_stage: string | null;
  failure_mode: string | null;
  gradient_text: string;
  proposed_edit: string;
  decision: PromptGradientDecision;
  decided_by: number | null;
  decided_at: string | null;
  decision_note: string | null;
  /**
   * builtin 编译器目前只写 source_badcase_count。渲染必须能处理未知 key，
   * 也绝不能在没有指标时假装有指标。
   */
  evaluation: Record<string, unknown>;
}

export interface PromptCompilerConfig {
  compiler: PromptCompilerName;
  max_patches: number;
  min_cluster_support: number;
  instruction_candidates: number;
  textgrad_iterations: number;
  demo_count: 0 | 2 | 4;
  redaction_mode: PromptRedactionMode;
  max_prompt_tokens: number;
  efficiency_policy: PromptEfficiencyPolicy;
  seed: number;
}

/** 四个字段都是上限（cap），不是预估花费。 */
export interface PromptCompileBudget {
  max_provider_calls: number;
  max_provider_tokens: number;
  max_cost_microunits: number;
  max_wall_seconds: number;
}

export interface CreatePromptCompilationRequest {
  baseline_tagger_version_id: number;
  gold_set_version_id?: number;
  compiler: PromptCompilerConfig;
  budget: PromptCompileBudget;
}

export interface CreatePromptCompilationResponse {
  compilation_id: number;
  job_id: number;
}

export interface PromptPatchDecisionItem {
  patch_id: string;
  decision: "accepted" | "rejected";
  note?: string;
}

/**
 * decisions 表达的是「最终采纳集」而非「本次改动」：未列出的补丁会被服务端的
 * rematerialize 当成拒绝而移除。提交时必须把未变更的补丁按现状一并重放。
 */
export interface PromptPatchDecisionBatch {
  decisions: PromptPatchDecisionItem[];
  dropped_demo_ids: string[];
}

export interface PromptArtifactPromoteRequest {
  /** ^[\w.-]+$，1–32 字；最终版本号为 `{基线版本}-lab-{suffix}`。 */
  version_suffix: string;
  /** 8–4000 字，落到候选版本的 change_summary。 */
  change_summary: string;
  efficiency_policy?: PromptEfficiencyPolicy;
}

/** 晋级只产出 draft 候选：仍要走标签治理的评估与部署门禁，绝不直通生产。 */
export interface PromptArtifactPromoteResponse {
  artifact: PromptArtifactSummary;
  candidate_tagger_version: {
    id: number;
    version: string;
    status: string;
    origin: string;
    prompt_artifact_id: number | null;
  };
}

export type PromptArtifactListResponse =
  TagGovernanceListResponse<PromptArtifactSummary>;
export type PromptGradientListResponse =
  TagGovernanceListResponse<PromptGradient>;
