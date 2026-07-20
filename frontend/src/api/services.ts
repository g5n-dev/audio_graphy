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
} from "@/types/api";

// ============================================================
// Auth
// ============================================================

export async function login(email: string, password: string): Promise<TokenResponse> {
  const { data } = await httpClient.post<TokenResponse>("/auth/login", { email, password });
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
  const { data } = await httpClient.get<RecordingListResponse>("/recordings", { params });
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
  const { data } = await httpClient.get<ExploreResponse>("/graph/explore", { params });
  return data;
}

export async function getEntity(name: string): Promise<EntityDetailResponse> {
  const { data } = await httpClient.get<EntityDetailResponse>(`/graph/entity/${encodeURIComponent(name)}`);
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

export async function getTags(recordingId: number, view: string = "current"): Promise<TagsListResponse> {
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

export async function query(text: string, topK: number = 10): Promise<QueryResponse> {
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
  const { data } = await httpClient.get<StatsResponse>("/tags/stats", { params });
  return data;
}
