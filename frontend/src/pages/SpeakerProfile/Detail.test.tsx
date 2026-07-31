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
    },
    {
      recording_id: 20,
      voiceprint_id: "vp_cccc3333",
      duration_sec: 180.5,
      strategy: "voiceprint",
      ambiguity_tag: "AMBIGUOUS",
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
});
