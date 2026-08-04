/**
 * RecordingDetailPage — pipeline progress panel (stage checklist from
 * required vs completed projections, plain error surfacing for failed
 * runs, admin retry) and the softened reception entry card.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RecordingDetailPage from "./RecordingDetailPage";
import { useAuthStore } from "@/stores/auth";
import type {
  PipelineRunResponse,
  RecordingResponse,
  SegmentListResponse,
  TagsListResponse,
} from "@/types/api";

vi.mock("@/api/services", () => ({
  getRecording: vi.fn(),
  getRecordingProcessingRun: vi.fn(),
  getSegments: vi.fn(),
  getTags: vi.fn(),
  reindexRecording: vi.fn(),
}));

import {
  getRecording,
  getRecordingProcessingRun,
  getSegments,
  getTags,
  reindexRecording,
} from "@/api/services";

const mockedGetRecording = getRecording as unknown as ReturnType<typeof vi.fn>;
const mockedGetRun = getRecordingProcessingRun as unknown as ReturnType<
  typeof vi.fn
>;
const mockedGetSegments = getSegments as unknown as ReturnType<typeof vi.fn>;
const mockedGetTags = getTags as unknown as ReturnType<typeof vi.fn>;
const mockedReindex = reindexRecording as unknown as ReturnType<typeof vi.fn>;

const RECORDING: RecordingResponse = {
  id: 7,
  tenant_id: "tenant-a",
  store_id: "store-1",
  agent_name: "销售甲",
  customer_hash: null,
  status: "failed",
  pipeline_state: "failed",
  recorded_at: "2026-07-30T10:00:00Z",
  prompt_version: null,
  indexed_at: null,
  created_at: "2026-07-30T10:05:00Z",
  segments_count: 0,
  chunks_count: 0,
  current_tags: [],
  active_pipeline_run_id: 12,
};

const FAILED_RUN: PipelineRunResponse = {
  id: 12,
  recording_id: 7,
  generation: 2,
  state: "failed_retryable",
  attempt_count: 3,
  required_projections: ["file_index", "vector", "graph"],
  completed_projections: ["file_index"],
  error_code: "ASR_TIMEOUT",
  error_message: "ASR provider timed out after 300s",
  lease_expires_at: null,
  started_at: "2026-07-30T10:00:00Z",
  finished_at: "2026-07-30T10:05:00Z",
  activated_at: null,
};

const SEGMENTS: SegmentListResponse = {
  recording_id: 7,
  items: [],
  total: 0,
  page: 1,
  page_size: 100,
};

const TAGS: TagsListResponse = {
  recording_id: 7,
  view: "current",
  tags: [],
};

function setUser(role: "admin" | "agent") {
  useAuthStore.setState({
    token: "t",
    refreshToken: "r",
    user: {
      id: 1,
      name: "操作员",
      email: "op@example.com",
      role,
      tenant_id: "tenant-a",
    },
    isAuthenticated: true,
  });
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/recordings/7"]}>
        <Routes>
          <Route path="/recordings/:id" element={<RecordingDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedGetRecording.mockReset();
  mockedGetRun.mockReset();
  mockedGetSegments.mockReset();
  mockedGetTags.mockReset();
  mockedReindex.mockReset();
  mockedGetRecording.mockResolvedValue(RECORDING);
  mockedGetRun.mockResolvedValue(FAILED_RUN);
  mockedGetSegments.mockResolvedValue(SEGMENTS);
  mockedGetTags.mockResolvedValue(TAGS);
  setUser("admin");
});

describe("RecordingDetailPage pipeline panel", () => {
  it("surfaces error_code and error_message for a failed run", async () => {
    renderPage();

    expect(await screen.findByText("处理失败")).toBeInTheDocument();
    expect(screen.getByText(/ASR_TIMEOUT/)).toBeInTheDocument();
    expect(
      screen.getByText("ASR provider timed out after 300s"),
    ).toBeInTheDocument();
    // Checklist derived from required vs completed projections.
    expect(screen.getByText("文件索引")).toBeInTheDocument();
    expect(screen.getByText("向量索引")).toBeInTheDocument();
    expect(screen.getByText("图谱投影")).toBeInTheDocument();
    expect(mockedGetRun).toHaveBeenCalledWith(7, 12);
  });

  it("lets an admin retry a failed run", async () => {
    const user = userEvent.setup();
    mockedReindex.mockResolvedValue({
      id: 7,
      status: "queued",
      pipeline_state: "queued",
      operation_id: 13,
      generation: 3,
      operation_state: "queued",
      message: "Reindex triggered",
    });
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: "重试处理" }),
    );

    await waitFor(() => {
      expect(mockedReindex).toHaveBeenCalledWith(7, { force: false });
    });
  });

  it("offers a force rerun while a run is still processing", async () => {
    mockedGetRecording.mockResolvedValue({
      ...RECORDING,
      status: "processing",
      pipeline_state: "asr",
    });
    mockedGetRun.mockResolvedValue({
      ...FAILED_RUN,
      state: "asr",
      error_code: null,
      error_message: null,
    });
    renderPage();

    expect(await screen.findByText("处理进行中")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "强制重跑" }),
    ).toBeInTheDocument();
  });

  it("hides retry controls from non-admin users", async () => {
    setUser("agent");
    renderPage();

    expect(await screen.findByText("处理失败")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重试处理" }),
    ).not.toBeInTheDocument();
  });

  it("renders no pipeline panel for an indexed recording", async () => {
    mockedGetRecording.mockResolvedValue({
      ...RECORDING,
      status: "indexed",
      pipeline_state: "indexed",
      indexed_at: "2026-07-30T10:10:00Z",
    });
    renderPage();

    expect(await screen.findByText("录音 #7")).toBeInTheDocument();
    expect(screen.queryByText("处理失败")).not.toBeInTheDocument();
    expect(screen.queryByText("处理进行中")).not.toBeInTheDocument();
    expect(mockedGetRun).not.toHaveBeenCalled();
  });
});

describe("RecordingDetailPage reception entry card", () => {
  it("explains the store/time lookup instead of linking a dead recording filter", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("录音 #7");
    await user.click(screen.getByRole("tab", { name: /接待/ }));

    // The queue has no server-side recording filter, so the card must not
    // pretend one exists.
    expect(
      await screen.findByText(/接待队列暂不支持按录音直接筛选/),
    ).toBeInTheDocument();
    const link = document.querySelector("a.ag-outlink-card__link");
    expect(link).not.toBeNull();
    // The card jumps into the store's queue (a focus type ReceptionEntry can
    // actually apply), never a recording focus the queue cannot filter by.
    expect(link).toHaveAttribute(
      "href",
      expect.stringContaining("focus=门店:store-1"),
    );
    expect(link?.getAttribute("href")).not.toContain("录音");
  });
});
