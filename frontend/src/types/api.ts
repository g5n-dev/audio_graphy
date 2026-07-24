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

export interface RecordingListItem {
  id: number;
  store_id: string;
  agent_name: string;
  status: string;
  pipeline_state: string;
  recorded_at: string | null;
  indexed_at: string | null;
  prompt_version: string | null;
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
  agent_name: string;
  customer_hash: string | null;
  status: string;
  pipeline_state: string;
  recorded_at: string | null;
  prompt_version: string | null;
  indexed_at: string | null;
  created_at: string;
  segments_count: number;
  chunks_count: number;
  current_tags: TagSummary[];
}

export interface RecordingListResponse {
  items: RecordingListItem[];
  total: number;
  page: number;
  page_size: number;
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
  description: string;
  degree: number;
  source_ids: string[];
  recording_ids: number[];
}

export interface GraphEdgeResponse {
  source: string;
  target: string;
  relation: string;
  weight: number;
  confidence: string;
  confidence_score: number | null;
  source_ids: string[];
}

export interface ExploreResponse {
  nodes: GraphNodeResponse[];
  edges: GraphEdgeResponse[];
  total_nodes: number;
  total_edges: number;
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

export interface PromptResponse extends PromptListItem {
  content: string;
}

// ============================================================
// Query
// ============================================================

export interface Citation {
  entity: string;
  chunk_id: string | null;
  segment_ids: number[];
  recording_id: number | null;
  recorded_at: string | null;
  transcript_snippet: string | null;
  confidence: number;
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
  voiceprint_id: string;
  duration_sec: number;
  strategy: string;
  ambiguity_tag: "AMBIGUOUS" | "PENDING_REVIEW" | null;
}

export interface SpeakerDetailResponse extends SpeakerListItem {
  recordings_list: number[];
  related_recordings: SpeakerRecordingRef[];
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
  version: number;
}

export interface ReceptionRecordingItem {
  id: EntityId;
  name: string;
  sequence_no: number;
  timeline_start_sec: number;
  timeline_end_sec: number;
  source_start_sec: number;
  source_end_sec: number | null;
  gap_before_sec: number;
  audio_url: string | null;
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

export interface ReceptionWorkspaceResponse {
  reception: ReceptionSummary;
  recordings: ReceptionRecordingItem[];
  dialogue_units: ReceptionDialogueUnit[];
  transcript_items: ReceptionTranscriptItem[];
  tag_assignments: ReceptionTagAssignment[];
  state_transitions: ReceptionStateTransition[];
  audit_events: ReceptionAuditEvent[];
  window: ReceptionWorkspaceWindow;
  /** Real normalized peaks only; omitted while the backend has not produced them. */
  waveform_peaks?: number[];
}

export interface MergeReceptionRecordingsRequest {
  recording_ids: EntityId[];
  mode: ReceptionMergeMode;
  expected_version: number;
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
  gap_before_sec: number;
  decision_source: "explicit" | "auto" | "manual";
  merge_confidence: number | null;
  merge_reasons: Record<string, unknown>;
  source_recorded_at: string | null;
  /** Short-lived browser playback URL supplied by the authorized audio endpoint. */
  audio_url: string;
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
  | ReceptionResponseApi
  | ReceptionSplitAcceptanceResponse;

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
  | "pending" | "running" | "failed" | "ready";
export type ReceptionAutomationStage =
  | "merge" | "segmentation" | "tagging" | "ready";

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
