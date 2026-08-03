/**
 * Speakers API client tests — M7 WS-3 T12.
 *
 * Mocks httpClient to verify:
 *   - Endpoint URL construction
 *   - Query parameter passing (role, ambiguity, limit, offset)
 *   - Response passthrough
 *   - Detail URL pattern (/speakers/:id)
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { listSpeakers, getSpeaker, getVoiceprintPolicy } from "./speakers";
import type {
  SpeakerDetailResponse,
  SpeakerListResponse,
  VoiceprintPolicyResponse,
} from "@/types/api";

// Mock the httpClient module
vi.mock("./client", () => ({
  httpClient: {
    get: vi.fn(),
  },
}));

// Import after mock so the mocked module is used
import { httpClient } from "./client";

const mockedGet = httpClient.get as unknown as ReturnType<typeof vi.fn>;

describe("speakers API client", () => {
  beforeEach(() => {
    mockedGet.mockReset();
  });

  it("listSpeakers calls /speakers with default params", async () => {
    const mockResponse: SpeakerListResponse = { items: [], total: 0 };
    mockedGet.mockResolvedValueOnce({ data: mockResponse });

    const result = await listSpeakers();
    expect(mockedGet).toHaveBeenCalledWith("/speakers", { params: undefined });
    expect(result).toEqual(mockResponse);
  });

  it("listSpeakers forwards role, ambiguity, limit, offset", async () => {
    mockedGet.mockResolvedValueOnce({ data: { items: [], total: 0 } });
    await listSpeakers({
      speaker_role: "agent",
      ambiguity: "AMBIGUOUS",
      limit: 50,
      offset: 100,
    });
    expect(mockedGet).toHaveBeenCalledWith("/speakers", {
      params: {
        speaker_role: "agent",
        ambiguity: "AMBIGUOUS",
        limit: 50,
        offset: 100,
      },
    });
  });

  it("getSpeaker calls /speakers/:id with numeric id", async () => {
    const mockDetail: SpeakerDetailResponse = {
      id: 42,
      tenant_id: "tenant-a",
      display_name: "Speaker 42",
      voiceprint_hash: "vp_abcdef12",
      speaker_role: "agent",
      recordings_count: 3,
      first_seen: "2024-01-01T00:00:00Z",
      total_speech_sec: 120.5,
      merge_confidence: 0.95,
      merge_strategy: "voiceprint",
      ambiguity_tag: null,
      recordings_list: [1, 2, 3],
      related_recordings: [],
    };
    mockedGet.mockResolvedValueOnce({ data: mockDetail });

    const result = await getSpeaker(42);
    expect(mockedGet).toHaveBeenCalledWith("/speakers/42");
    expect(result).toEqual(mockDetail);
  });

  it("propagates errors from httpClient", async () => {
    const err = new Error("network");
    mockedGet.mockRejectedValueOnce(err);
    await expect(getSpeaker(99)).rejects.toThrow("network");
  });

  it("getVoiceprintPolicy calls /speakers/voiceprint-policy", async () => {
    const mockPolicy: VoiceprintPolicyResponse = {
      enable_voiceprint: false,
      adapter_voiceprint_mode: "mock",
      layer1: { cosine_threshold: 0.5, ambiguous_threshold: 0.7 },
      layer2: {
        enabled: true,
        fuzzy_inferred_threshold: 0.6,
        fuzzy_ambiguous_threshold: 0.85,
        voiceprint_reconfirm_cosine: 0.7,
      },
      sampling: {
        strategy: "weighted_mean",
        min_segment_sec: 0.5,
        min_total_sec: 3,
        max_segments_per_speaker: 8,
        diarization_min_segment_sec: 0.5,
        max_speakers: 10,
        embedding_dim: 192,
      },
      retention_cascade: true,
    };
    mockedGet.mockResolvedValueOnce({ data: mockPolicy });

    const result = await getVoiceprintPolicy();
    expect(mockedGet).toHaveBeenCalledWith("/speakers/voiceprint-policy");
    expect(result).toEqual(mockPolicy);
  });
});
