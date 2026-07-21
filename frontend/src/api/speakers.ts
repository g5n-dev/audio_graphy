/**
 * Speakers API client — M7 WS-3 T12.
 *
 * Wraps the backend `/api/v1/speakers` endpoints. All functions return
 * typed responses; throws on non-2xx (handled by httpClient interceptor).
 */

import { httpClient } from "./client";
import type {
  SpeakerDetailResponse,
  SpeakerListResponse,
} from "@/types/api";

export interface ListSpeakersParams {
  speaker_role?: "agent" | "customer" | "unknown";
  ambiguity?: "AMBIGUOUS" | "PENDING_REVIEW" | "none";
  limit?: number;
  offset?: number;
}

export async function listSpeakers(
  params?: ListSpeakersParams,
): Promise<SpeakerListResponse> {
  const { data } = await httpClient.get<SpeakerListResponse>("/speakers", {
    params,
  });
  return data;
}

export async function getSpeaker(id: number): Promise<SpeakerDetailResponse> {
  const { data } = await httpClient.get<SpeakerDetailResponse>(
    `/speakers/${id}`,
  );
  return data;
}
