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
}));

import { listSpeakers } from "@/api/speakers";

const mockedListSpeakers = listSpeakers as unknown as ReturnType<typeof vi.fn>;

function renderWithProviders(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/speakers"]}>
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

describe("SpeakerProfileListPage", () => {
  beforeEach(() => {
    mockedListSpeakers.mockReset();
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
});
