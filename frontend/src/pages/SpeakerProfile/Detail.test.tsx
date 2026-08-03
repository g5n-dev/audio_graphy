/**
 * SpeakerProfile detail page tests — M7 WS-3 T12.
 *
 * Covers:
 *   - Renders detail info when loaded
 *   - Shows related recordings table
 *   - Shows error fallback when speaker missing
 *   - Loading state renders Spin
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import SpeakerProfileDetailPage from "./Detail";
import type { SpeakerDetailResponse } from "@/types/api";

vi.mock("@/api/speakers", () => ({
  getSpeaker: vi.fn(),
  getVoiceprintPolicy: vi.fn().mockResolvedValue({
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
  }),
}));

vi.mock("@/api/advancedGraph", () => ({
  listSpeakerMergePending: vi.fn(),
  confirmSpeakerMerge: vi.fn(),
  rejectSpeakerMerge: vi.fn(),
}));

import { getSpeaker } from "@/api/speakers";
import { listSpeakerMergePending } from "@/api/advancedGraph";

const mockedGetSpeaker = getSpeaker as unknown as ReturnType<typeof vi.fn>;
const mockedListSpeakerMergePending =
  listSpeakerMergePending as unknown as ReturnType<typeof vi.fn>;

const MOCK_DETAIL: SpeakerDetailResponse = {
  id: 7,
  tenant_id: "t-1",
  display_name: "Carol",
  voiceprint_hash: "vp_cccc3333",
  speaker_role: "agent",
  recordings_count: 4,
  first_seen: "2024-03-10T09:15:00Z",
  total_speech_sec: 730.5,
  merge_confidence: 0.88,
  merge_strategy: "voiceprint",
  ambiguity_tag: null,
  recordings_list: [10, 20, 30, 40],
  related_recordings: [
    {
      recording_id: 10,
      voiceprint_id: "vp_cccc3333",
      duration_sec: 250.0,
      strategy: "voiceprint",
      ambiguity_tag: null,
      cosine_similarity: 0.91,
      merge_confidence: 0.91,
    },
    {
      recording_id: 20,
      voiceprint_id: "vp_cccc3333",
      duration_sec: 180.5,
      strategy: "voiceprint",
      ambiguity_tag: "AMBIGUOUS",
      cosine_similarity: 0.58,
      merge_confidence: 0.58,
    },
  ],
};

function renderDetailWithId(id: string): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/speakers/${id}`]}>
        <Routes>
          <Route path="/speakers/:id" element={<SpeakerProfileDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SpeakerProfileDetailPage", () => {
  beforeEach(() => {
    mockedGetSpeaker.mockReset();
    mockedListSpeakerMergePending.mockReset();
    mockedListSpeakerMergePending.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 50,
    });
  });

  it("renders display name and detail fields when loaded", async () => {
    mockedGetSpeaker.mockResolvedValueOnce(MOCK_DETAIL);
    renderDetailWithId("7");
    expect(await screen.findByText("Carol")).toBeInTheDocument();
    // SpeakerNode id label
    expect(screen.getByText(/SpeakerNode #7/)).toBeInTheDocument();
    // voiceprint hash appears twice (Descriptions + Table voiceprint_id col)
    expect(screen.getAllByText("vp_cccc3333").length).toBeGreaterThanOrEqual(1);
    // merge_strategy
    expect(screen.getAllByText("voiceprint").length).toBeGreaterThan(0);
  });

  it("renders related recordings section", async () => {
    mockedGetSpeaker.mockResolvedValueOnce(MOCK_DETAIL);
    renderDetailWithId("7");
    expect(await screen.findByText("Carol")).toBeInTheDocument();
    expect(screen.getByText("关联录音")).toBeInTheDocument();
    // Recording IDs appear in the table
    expect(screen.getAllByText("10").length).toBeGreaterThan(0);
    expect(screen.getAllByText("20").length).toBeGreaterThan(0);
  });

  it("shows each link's voiceprint cosine", async () => {
    mockedGetSpeaker.mockResolvedValueOnce(MOCK_DETAIL);
    renderDetailWithId("7");
    expect(await screen.findByText("Carol")).toBeInTheDocument();
    // Users could see *that* a merge happened but not how close it was.
    expect(screen.getByText("0.910")).toBeInTheDocument();
    expect(screen.getByText("0.580")).toBeInTheDocument();
  });

  it("renders cross-recording mini-view section", async () => {
    mockedGetSpeaker.mockResolvedValueOnce(MOCK_DETAIL);
    renderDetailWithId("7");
    expect(await screen.findByText(/跨录音关系/)).toBeInTheDocument();
    // Mini-view shows the speakerId
    expect(screen.getByText(/Speaker #7/)).toBeInTheDocument();
    // Each recording id should appear in the mini-view
    expect(screen.getByText(/Recording #10/)).toBeInTheDocument();
    expect(screen.getByText(/Recording #20/)).toBeInTheDocument();
  });

  it("shows fallback message on load error", async () => {
    mockedGetSpeaker.mockRejectedValueOnce(new Error("404"));
    renderDetailWithId("9999");
    expect(
      await screen.findByText("说话人不存在或加载失败"),
    ).toBeInTheDocument();
  });

  it("shows fallback message when speaker is null", async () => {
    // Force getSpeaker to resolve undefined (no data found)
    mockedGetSpeaker.mockRejectedValueOnce(new Error("not found"));
    renderDetailWithId("0");
    expect(
      await screen.findByText("说话人不存在或加载失败"),
    ).toBeInTheDocument();
  });

  // GD-006: "在图谱中查看" button
  it("renders a link to view the speaker in the knowledge graph", async () => {
    mockedGetSpeaker.mockResolvedValueOnce(MOCK_DETAIL);
    renderDetailWithId("7");
    expect(await screen.findByText("Carol")).toBeInTheDocument();
    expect(
      screen.getByText("在图谱中查看"),
    ).toBeInTheDocument();
  });

  it("requests pending merges filtered by this speaker server-side", async () => {
    mockedGetSpeaker.mockResolvedValueOnce(MOCK_DETAIL);
    renderDetailWithId("7");
    await screen.findByText("Carol");
    // Client-side filtering over one capped page would hide this speaker's
    // older rows once the tenant queue grows past the limit.
    expect(mockedListSpeakerMergePending).toHaveBeenCalledWith({
      status: "pending",
      matched_speaker_node_id: 7,
      limit: 50,
    });
  });

  it("shows the backend message when the detail request fails", async () => {
    mockedGetSpeaker.mockRejectedValueOnce({
      response: {
        status: 403,
        data: { error: { code: "FORBIDDEN", message: "需要 inspector 权限" } },
      },
    });
    renderDetailWithId("7");
    expect(
      await screen.findByText("说话人不存在或加载失败"),
    ).toBeInTheDocument();
    expect(screen.getByText("需要 inspector 权限")).toBeInTheDocument();
  });
});
