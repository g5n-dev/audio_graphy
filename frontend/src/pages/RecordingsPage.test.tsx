/**
 * RecordingsPage — ingest entry (导入录音), progress polling, and failed-row
 * retry. These are the three previously missing links of the ingest chain,
 * so each has a test that fails if the wiring is removed again.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RecordingsPage from "./RecordingsPage";
import { recordingsPollInterval } from "@/utils/recordingsPolling";
import { useAuthStore } from "@/stores/auth";
import type { RecordingListResponse, RecordingResponse } from "@/types/api";

vi.mock("@/api/services", () => ({
  createRecording: vi.fn(),
  listRecordings: vi.fn(),
  reindexRecording: vi.fn(),
}));

import {
  createRecording,
  listRecordings,
  reindexRecording,
} from "@/api/services";

const mockedCreate = createRecording as unknown as ReturnType<typeof vi.fn>;
const mockedList = listRecordings as unknown as ReturnType<typeof vi.fn>;
const mockedReindex = reindexRecording as unknown as ReturnType<typeof vi.fn>;

const LIST: RecordingListResponse = {
  items: [
    {
      id: 1,
      store_id: "store-1",
      agent_name: "销售甲",
      status: "failed",
      pipeline_state: "failed",
      recorded_at: "2026-07-30T10:00:00Z",
      indexed_at: null,
      prompt_version: null,
      active_pipeline_run_id: 11,
    },
    {
      id: 2,
      store_id: "store-1",
      agent_name: "销售乙",
      status: "processing",
      pipeline_state: "asr",
      recorded_at: "2026-07-30T11:00:00Z",
      indexed_at: null,
      prompt_version: null,
      active_pipeline_run_id: 12,
    },
  ],
  total: 2,
  page: 1,
  page_size: 20,
};

const CREATED: RecordingResponse = {
  id: 99,
  tenant_id: "tenant-a",
  store_id: "store-1",
  agent_name: null,
  customer_hash: null,
  status: "queued",
  pipeline_state: "queued",
  recorded_at: null,
  prompt_version: null,
  indexed_at: null,
  created_at: "2026-07-31T00:00:00Z",
  segments_count: 0,
  chunks_count: 0,
  current_tags: [],
  active_pipeline_run_id: 21,
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
      <MemoryRouter initialEntries={["/recordings"]}>
        <Routes>
          <Route path="/recordings" element={<RecordingsPage />} />
          <Route
            path="/recordings/:id"
            element={<div data-testid="detail-page" />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedCreate.mockReset();
  mockedList.mockReset();
  mockedReindex.mockReset();
  mockedList.mockResolvedValue(LIST);
  setUser("admin");
});

describe("recordingsPollInterval", () => {
  it("polls while any row is queued or processing", () => {
    expect(recordingsPollInterval(LIST.items)).toBe(5000);
    expect(
      recordingsPollInterval([{ status: "queued" }]),
    ).toBe(5000);
  });

  it("stops polling once every row is settled", () => {
    expect(
      recordingsPollInterval([{ status: "indexed" }, { status: "failed" }]),
    ).toBe(false);
    expect(recordingsPollInterval([])).toBe(false);
    expect(recordingsPollInterval(undefined)).toBe(false);
  });
});

describe("RecordingsPage import entry", () => {
  it("lets an admin register a server-side file and jump to its detail", async () => {
    const user = userEvent.setup();
    mockedCreate.mockResolvedValue(CREATED);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "导入录音" }));
    // The dialog must explain that the path lives on the server, not the
    // operator's machine.
    expect(
      await screen.findByText(/服务器（后端工作目录/),
    ).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("例如 store-001"), "store-1");
    await user.type(
      screen.getByPlaceholderText("mock_data/demo.wav"),
      "mock_data/demo.wav",
    );
    await user.click(screen.getByRole("button", { name: "登记录音" }));

    await waitFor(() => {
      expect(mockedCreate).toHaveBeenCalledWith({
        store_id: "store-1",
        path: "mock_data/demo.wav",
      });
    });
    // Success navigates to the new recording's detail page.
    expect(await screen.findByTestId("detail-page")).toBeInTheDocument();
  });

  it("hides the import entry from non-admin users", async () => {
    setUser("agent");
    renderPage();

    await screen.findByText("销售甲");
    expect(
      screen.queryByRole("button", { name: "导入录音" }),
    ).not.toBeInTheDocument();
  });
});

describe("RecordingsPage retry actions", () => {
  it("retries a failed row without force", async () => {
    const user = userEvent.setup();
    mockedReindex.mockResolvedValue({
      id: 1,
      status: "queued",
      pipeline_state: "queued",
      operation_id: 31,
      generation: 2,
      operation_state: "queued",
      message: "Reindex triggered",
    });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "重试" }));

    await waitFor(() => {
      expect(mockedReindex).toHaveBeenCalledWith(1, { force: false });
    });
  });

  it("force-reruns a stuck processing row", async () => {
    const user = userEvent.setup();
    mockedReindex.mockResolvedValue({
      id: 2,
      status: "queued",
      pipeline_state: "queued",
      operation_id: 32,
      generation: 3,
      operation_state: "queued",
      message: "Reindex triggered",
    });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "强制重跑" }));

    await waitFor(() => {
      expect(mockedReindex).toHaveBeenCalledWith(2, { force: true });
    });
  });

  it("hides retry actions from non-admin users", async () => {
    setUser("agent");
    renderPage();

    await screen.findByText("销售甲");
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "强制重跑" }),
    ).not.toBeInTheDocument();
  });
});
