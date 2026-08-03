/**
 * SpeakerProfile list page tests — M7 WS-3 T12.
 *
 * Covers:
 *   - Renders title and empty state
 *   - Renders table rows from mocked query
 *   - voiceprint_hash column shows truncated hash
 *   - recordings_count column
 *   - listSpeakers called with proper params
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import SpeakerProfileListPage from "./index";
import type { SpeakerListResponse } from "@/types/api";

// Mock speakers API
vi.mock("@/api/speakers", () => ({
  listSpeakers: vi.fn(),
  getVoiceprintPolicy: vi.fn(),
}));

vi.mock("@/api/advancedGraph", () => ({
  listSpeakerMergePending: vi.fn(),
  confirmSpeakerMerge: vi.fn(),
  rejectSpeakerMerge: vi.fn(),
}));

import { getVoiceprintPolicy, listSpeakers } from "@/api/speakers";
import { listSpeakerMergePending } from "@/api/advancedGraph";

const mockedListSpeakers = listSpeakers as unknown as ReturnType<typeof vi.fn>;
const mockedGetVoiceprintPolicy =
  getVoiceprintPolicy as unknown as ReturnType<typeof vi.fn>;
const mockedListSpeakerMergePending =
  listSpeakerMergePending as unknown as ReturnType<typeof vi.fn>;

function renderWithProviders(entry = "/speakers"): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/speakers" element={<SpeakerProfileListPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const MOCK_SPEAKERS: SpeakerListResponse = {
  items: [
    {
      id: 1,
      tenant_id: "t-1",
      display_name: "Alice",
      voiceprint_hash: "vp_aaaa1111",
      speaker_role: "agent",
      recordings_count: 5,
      first_seen: "2024-01-15T08:30:00Z",
      total_speech_sec: 600.2,
      merge_confidence: 0.92,
      merge_strategy: "voiceprint",
      ambiguity_tag: null,
    },
    {
      id: 2,
      tenant_id: "t-1",
      display_name: "Bob",
      voiceprint_hash: "vp_bbbb2222",
      speaker_role: "customer",
      recordings_count: 2,
      first_seen: "2024-02-01T10:00:00Z",
      total_speech_sec: 180.0,
      merge_confidence: 0.55,
      merge_strategy: "fuzzy",
      ambiguity_tag: "AMBIGUOUS",
    },
  ],
  total: 2,
};

const MOCK_POLICY = {
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

describe("SpeakerProfileListPage", () => {
  beforeEach(() => {
    mockedListSpeakers.mockReset();
    mockedGetVoiceprintPolicy.mockReset();
    mockedGetVoiceprintPolicy.mockResolvedValue(MOCK_POLICY);
    mockedListSpeakerMergePending.mockReset();
    mockedListSpeakerMergePending.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 1,
    });
  });

  it("renders title 说话人管理", async () => {
    mockedListSpeakers.mockResolvedValueOnce({ items: [], total: 0 });
    renderWithProviders();
    expect(screen.getByText("说话人管理")).toBeInTheDocument();
  });

  it("renders speaker rows from query result", async () => {
    mockedListSpeakers.mockResolvedValueOnce(MOCK_SPEAKERS);
    renderWithProviders();
    // Wait for query to resolve and table to render
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Bob")).toBeInTheDocument();
    // Both voiceprint hashes should be rendered (truncated form already in data)
    expect(screen.getByText("vp_aaaa1111")).toBeInTheDocument();
    expect(screen.getByText("vp_bbbb2222")).toBeInTheDocument();
  });

  it("shows recordings_count column values", async () => {
    mockedListSpeakers.mockResolvedValueOnce(MOCK_SPEAKERS);
    renderWithProviders();
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    // recordings_count column renders these as text
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("calls listSpeakers with undefined role/ambiguity (no filter) and limit=200", async () => {
    mockedListSpeakers.mockResolvedValueOnce({ items: [], total: 0 });
    renderWithProviders();
    await screen.findByText("说话人管理");
    expect(mockedListSpeakers).toHaveBeenCalledTimes(1);
    const callArgs = mockedListSpeakers.mock.calls[0][0];
    expect(callArgs).toEqual({
      speaker_role: undefined,
      ambiguity: undefined,
      limit: 200,
    });
  });

  it("shows merge_strategy column with voiceprint/fuzzy values", async () => {
    mockedListSpeakers.mockResolvedValueOnce(MOCK_SPEAKERS);
    renderWithProviders();
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("voiceprint")).toBeInTheDocument();
    expect(screen.getByText("fuzzy")).toBeInTheDocument();
  });

  it("narrows the roster to a recording when arriving with ?focus=录音:N", async () => {
    mockedListSpeakers.mockResolvedValueOnce({ items: [], total: 0 });
    renderWithProviders(`/speakers?focus=${encodeURIComponent("录音")}%3A42`);
    expect(await screen.findByText(/录音 #42/)).toBeInTheDocument();
    expect(mockedListSpeakers).toHaveBeenCalledWith({
      speaker_role: undefined,
      ambiguity: undefined,
      recording_id: 42,
      limit: 200,
    });
  });

  it("preselects the role for an entity focus and says why it cannot filter", async () => {
    mockedListSpeakers.mockResolvedValueOnce({ items: [], total: 0 });
    renderWithProviders(`/speakers?focus=${encodeURIComponent("客户")}%3A%E7%8E%8B%E5%B0%8F%E5%A7%90`);
    // Display names are voiceprint hashes, so an honest banner beats a
    // silently unfiltered list.
    expect(await screen.findByText(/无法按姓名精确匹配/)).toBeInTheDocument();
    expect(mockedListSpeakers).toHaveBeenCalledWith({
      speaker_role: "customer",
      ambiguity: undefined,
      recording_id: undefined,
      limit: 200,
    });
  });

  it("renders the 声纹质量 entry button with pending badge count", async () => {
    mockedListSpeakers.mockResolvedValueOnce({ items: [], total: 0 });
    mockedListSpeakerMergePending.mockResolvedValue({
      items: [],
      total: 3,
      page: 1,
      page_size: 1,
    });
    renderWithProviders();
    expect(await screen.findByText("声纹质量")).toBeInTheDocument();
    // Badge renders the pending total from the merge-pending endpoint.
    expect(await screen.findByText("3")).toBeInTheDocument();
    expect(mockedListSpeakerMergePending).toHaveBeenCalledWith({
      status: "pending",
      limit: 1,
    });
  });
});
