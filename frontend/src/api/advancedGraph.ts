/**
 * M9 R2 Advanced-Graph API client.
 *
 * Wraps every R2 endpoint exposed by the backend under
 * `/api/v1/{recordings,admin,search,speakers}`. All functions return
 * typed responses; non-2xx throws (handled centrally by httpClient).
 *
 * L9: when the backend has `enable_advanced_graph=False` every call
 * here will receive a 404. Callers should catch and degrade gracefully.
 */

import { httpClient } from "./client";

// ============================================================
// Shared types
// ============================================================

export interface EdgeOut {
  source: string;
  target: string;
  relation: string;
  weight: number;
  confidence: string;
  confidence_score: number | null;
  source_ids: string[];
  valid_at: string | null;
  invalid_at: string | null;
  created_at: string | null;
  expired_at: string | null;
  superseded_by: string | null;
}

export interface EdgeEventOut {
  id: number;
  event_type: string;
  edge_key: string;
  source: string;
  target: string;
  relation: string;
  valid_at: string;
  invalid_at: string | null;
  superseded_by: string | null;
  actor: string;
  occurred_at: string | null;
}

export interface TimeTravelResponse {
  recording_id: number;
  as_of: string;
  edges: EdgeOut[];
  total: number;
}

export interface EdgeHistoryResponse {
  recording_id: number;
  edge_key: string;
  events: EdgeEventOut[];
  total: number;
}

export interface EdgeRangeQueryResponse {
  recording_id: number;
  from_time: string;
  to_time: string;
  edges: EdgeOut[];
  total: number;
}

// ============================================================
// Leiden admin types
// ============================================================

export interface LeidenJobOut {
  id: number;
  tenant_id: string;
  job_type: string;
  status: string;
  triggered_by: string;
  node_count_snapshot: number;
  edge_count_snapshot: number;
  diff_percent: number | null;
  modularity: number | null;
  levels: number;
  snapshot_path: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
}

export interface LeidenJobListResponse {
  items: LeidenJobOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface LeidenStatusResponse {
  tenant_id: string;
  last_job: LeidenJobOut | null;
  snapshot_exists: boolean;
  snapshot_path: string | null;
  enabled: boolean;
}

// ============================================================
// Search types
// ============================================================

export interface CommunityHit {
  community_id: number;
  level: number;
  title: string;
  summary: string;
  score: number;
  member_count: number;
}

export interface GlobalSearchResponse {
  query: string;
  level: number;
  hits: CommunityHit[];
  total: number;
  took_ms: number;
}

export interface LocalSearchHit {
  entity_id: string;
  name: string;
  type: string;
  description: string;
  score: number;
}

export interface LocalSearchResponse {
  query: string;
  seed_entity_ids: string[];
  depth: number;
  hits: LocalSearchHit[];
  total: number;
  took_ms: number;
}

export interface DrillDownResponse {
  community_id: number;
  parent_level: number;
  child_level: number;
  children: CommunityHit[];
  total: number;
}

// ============================================================
// Global graph topic-cluster types
// ============================================================

export interface TopicClusterJob {
  id: number;
  status: "succeeded";
  job_type: "full" | "incremental";
  modularity: number | null;
  finished_at: string | null;
}

export interface TopicCluster {
  community_id: number;
  level: number;
  title: string;
  summary: string;
  member_count: number;
  member_node_ids: string[];
}

export interface TopicClustersResponse {
  job: TopicClusterJob;
  available_jobs: TopicClusterJob[];
  level: number;
  clusters: TopicCluster[];
  total_clusters: number;
  total_members: number;
  generated_at: string | null;
}

export interface TopicClusterDetailResponse {
  job: TopicClusterJob;
  cluster: TopicCluster;
  related_clusters: TopicCluster[];
}

// ============================================================
// Compression types
// ============================================================

export interface CompressionCandidateOut {
  entity_id: string;
  score: number;
  reason: string;
}

export interface CompressionDryRunResponse {
  tenant_id: string;
  candidates: CompressionCandidateOut[];
  total: number;
}

export interface CompressionRunResponse {
  tenant_id: string;
  candidates: CompressionCandidateOut[];
  soft_deleted_nodes: string[];
  soft_deleted_edges: string[];
  rolled_back: boolean;
  error: string | null;
}

export interface CompressionHistoryItem {
  action: string;
  occurred_at: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  user_id: number | null;
}

export interface CompressionHistoryResponse {
  items: CompressionHistoryItem[];
  total: number;
  page: number;
  page_size: number;
}

// ============================================================
// Speaker merge-pending types
// ============================================================

export interface SpeakerMergePendingListItem {
  id: number;
  recording_id: number;
  candidate_name: string;
  matched_speaker_node_id: number;
  fuzzy_score: number;
  status: string;
  voiceprint_score: number | null;
  resolved_by: string | null;
  resolved_at: string | null;
  notes: string | null;
  created_at: string | null;
}

export interface SpeakerMergePendingListResponse {
  items: SpeakerMergePendingListItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface SpeakerConfirmMergeResponse {
  pending_id: number;
  status: string;
  resolved_by: string;
  voiceprint_score: number | null;
}

// ============================================================
// T4 — bi-temporal edge API
// ============================================================

export async function timeTravelEdges(
  recordingId: number,
  params: { at?: string; include_soft_deleted?: boolean },
): Promise<TimeTravelResponse> {
  const { data } = await httpClient.get<TimeTravelResponse>(
    `/recordings/${recordingId}/edges`,
    { params },
  );
  return data;
}

export async function edgesInRange(
  recordingId: number,
  params: { from: string; to: string },
): Promise<EdgeRangeQueryResponse> {
  const { data } = await httpClient.get<EdgeRangeQueryResponse>(
    `/recordings/${recordingId}/edges/range`,
    { params },
  );
  return data;
}

export async function edgeHistory(
  recordingId: number,
  edgeId: string,
  params?: { limit?: number; offset?: number },
): Promise<EdgeHistoryResponse> {
  const { data } = await httpClient.get<EdgeHistoryResponse>(
    `/recordings/${recordingId}/edges/${encodeURIComponent(edgeId)}/history`,
    { params },
  );
  return data;
}

// ============================================================
// T6 — Leiden admin
// ============================================================

export async function recomputeLeiden(
  body: { force_full?: boolean; triggered_by?: string },
): Promise<LeidenJobOut> {
  const { data } = await httpClient.post<LeidenJobOut>(
    "/admin/leiden/recompute",
    body,
  );
  return data;
}

export async function getLeidenJob(jobId: number): Promise<LeidenJobOut> {
  const { data } = await httpClient.get<LeidenJobOut>(
    `/admin/leiden/jobs/${jobId}`,
  );
  return data;
}

export async function listLeidenJobs(
  params?: { status?: string; limit?: number; offset?: number },
): Promise<LeidenJobListResponse> {
  const { data } = await httpClient.get<LeidenJobListResponse>(
    "/admin/leiden/jobs",
    { params },
  );
  return data;
}

export async function leidenStatus(): Promise<LeidenStatusResponse> {
  const { data } = await httpClient.get<LeidenStatusResponse>(
    "/admin/leiden/status",
  );
  return data;
}

// ============================================================
// T8 — search
// ============================================================

export async function globalSearch(
  body: {
    query: string;
    top_k?: number;
    level?: number;
    community_ids?: number[];
  },
): Promise<GlobalSearchResponse> {
  const { data } = await httpClient.post<GlobalSearchResponse>(
    "/search/global",
    body,
  );
  return data;
}

export async function localSearch(
  body: {
    query: string;
    seed_entity_ids: string[];
    depth?: number;
    top_k?: number;
  },
): Promise<LocalSearchResponse> {
  const { data } = await httpClient.post<LocalSearchResponse>(
    "/search/local",
    body,
  );
  return data;
}

export async function drillDown(
  communityId: number,
  body: { level?: number },
): Promise<DrillDownResponse> {
  const { data } = await httpClient.post<DrillDownResponse>(
    `/search/communities/${communityId}/drill-down`,
    body,
  );
  return data;
}

export async function getTopicClusters(params: {
  job_id?: number;
  level?: number;
  query?: string;
}): Promise<TopicClustersResponse> {
  const { data } = await httpClient.get<TopicClustersResponse>(
    "/graph/topic-clusters",
    { params },
  );
  return data;
}

export async function getTopicClusterDetail(
  jobId: number,
  level: number,
  communityId: number,
): Promise<TopicClusterDetailResponse> {
  const { data } = await httpClient.get<TopicClusterDetailResponse>(
    `/graph/topic-clusters/${jobId}/${level}/${communityId}`,
  );
  return data;
}

// ============================================================
// T10 — compression admin
// ============================================================

export async function compressionDryRun(
  body: {
    max_candidates?: number;
    god_node_degree_threshold?: number;
    stale_days?: number;
  },
): Promise<CompressionDryRunResponse> {
  const { data } = await httpClient.post<CompressionDryRunResponse>(
    "/admin/compression/dry-run",
    body,
  );
  return data;
}

export async function compressionRun(
  body: { max_candidates?: number; policy_check?: boolean },
): Promise<CompressionRunResponse> {
  const { data } = await httpClient.post<CompressionRunResponse>(
    "/admin/compression/run",
    body,
  );
  return data;
}

export async function compressionHistory(
  params?: { limit?: number; offset?: number },
): Promise<CompressionHistoryResponse> {
  const { data } = await httpClient.get<CompressionHistoryResponse>(
    "/admin/compression/history",
    { params },
  );
  return data;
}

// ============================================================
// T13 — speaker merge-pending
// ============================================================

export async function listSpeakerMergePending(
  params?: { status?: string; limit?: number; offset?: number },
): Promise<SpeakerMergePendingListResponse> {
  const { data } = await httpClient.get<SpeakerMergePendingListResponse>(
    "/speakers/merge-pending",
    { params },
  );
  return data;
}

export async function confirmSpeakerMerge(
  speakerId: number,
  targetId: number,
  body: { voiceprint_score?: number; notes?: string },
): Promise<SpeakerConfirmMergeResponse> {
  const { data } = await httpClient.post<SpeakerConfirmMergeResponse>(
    `/speakers/${speakerId}/merge/${targetId}`,
    body,
  );
  return data;
}

export async function rejectSpeakerMerge(
  speakerId: number,
  body: { notes?: string },
): Promise<SpeakerConfirmMergeResponse> {
  const { data } = await httpClient.post<SpeakerConfirmMergeResponse>(
    `/speakers/${speakerId}/reject-merge`,
    body,
  );
  return data;
}
