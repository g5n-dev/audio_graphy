/**
 * Speakers API client — M7 WS-3 T12.
 *
 * Wraps the backend `/api/v1/speakers` endpoints. All functions return
 * typed responses; throws on non-2xx (handled by httpClient interceptor).
 */

import { httpClient } from "./client";
import type {
  RecordingSpeakerListResponse,
  SpeakerDetailResponse,
  SpeakerListResponse,
  VoiceprintPolicyResponse,
} from "@/types/api";

export interface ListSpeakersParams {
  speaker_role?: "agent" | "customer" | "unknown";
  ambiguity?: "AMBIGUOUS" | "PENDING_REVIEW" | "none";
  /** Only speakers linked to this recording. */
  recording_id?: number;
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

/**
 * Resolve a recording's `spk_N` labels to canonical speakers.
 *
 * Segments only carry the per-file label, so any per-segment speaker display
 * needs this to show an identity or a confidence at all.
 */
export async function getRecordingSpeakers(
  recordingId: number,
): Promise<RecordingSpeakerListResponse> {
  const { data } = await httpClient.get<RecordingSpeakerListResponse>(
    `/recordings/${recordingId}/speakers`,
  );
  return data;
}

export async function getVoiceprintPolicy(): Promise<VoiceprintPolicyResponse> {
  const { data } = await httpClient.get<VoiceprintPolicyResponse>(
    "/speakers/voiceprint-policy",
  );
  return data;
}
