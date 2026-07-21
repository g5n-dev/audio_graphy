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

export interface MeResponse extends UserInfo {}

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
  path: string;
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
