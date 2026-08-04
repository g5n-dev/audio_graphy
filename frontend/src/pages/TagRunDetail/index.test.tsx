import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TagRunDetailPage from "./index";

vi.mock("@/api/services", () => ({
  cancelTagJob: vi.fn(),
  getTagJob: vi.fn(),
  retryTagJob: vi.fn(),
}));

import { cancelTagJob, getTagJob, retryTagJob } from "@/api/services";

const mockedCancel = cancelTagJob as unknown as ReturnType<typeof vi.fn>;
const mockedGet = getTagJob as unknown as ReturnType<typeof vi.fn>;
const mockedRetry = retryTagJob as unknown as ReturnType<typeof vi.fn>;

const RUN = {
  id: 88,
  tenant_id: "tenant-a",
  job_type: "extract",
  scope: { reception_ids: [101, 102], label_keys: ["intent", "objection"] },
  tagger_version_id: 42,
  status: "running",
  total_items: 100,
  completed_items: 64,
  failed_items: 3,
  failed_subset: [
    102,
    {
      subject_type: "dialogue_unit",
      subject_id: 77,
      error_code: "INVALID_OUTPUT",
    },
  ],
  attempt_count: 1,
  max_attempts: 3,
  revision: 4,
  lease_owner: "worker-a",
  lease_expires_at: "2026-07-25T06:00:00Z",
  next_attempt_at: null,
  last_error_code: null,
  last_error_message: null,
  created_at: "2026-07-25T05:00:00Z",
  updated_at: "2026-07-25T05:30:00Z",
  finished_at: null,
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/tag-runs/88"]}>
        <Routes>
          <Route path="/tag-runs/:id" element={<TagRunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TagRunDetailPage", () => {
  beforeEach(() => {
    mockedCancel.mockReset();
    mockedGet.mockReset();
    mockedRetry.mockReset();
    mockedGet.mockResolvedValue(RUN);
    mockedRetry.mockResolvedValue({ ...RUN, status: "queued", attempt_count: 2 });
    mockedCancel.mockResolvedValue({
      ...RUN,
      status: "cancelled",
      lease_owner: null,
      lease_expires_at: null,
    });
  });

  it("renders run scope, progress and checkpoints without inventing completion", async () => {
    renderPage();
    expect(
      await screen.findByRole("heading", { name: "抽取运行 #88" }),
    ).toBeInTheDocument();
    expect(screen.getByText("64 / 100")).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "运行完成度" }),
    ).toHaveAttribute("aria-valuenow", "64");
    expect(screen.getByText("模型抽取")).toBeInTheDocument();
    expect(screen.getByText("接待 101、102")).toBeInTheDocument();
    expect(screen.getByText("intent、objection")).toBeInTheDocument();
    expect(screen.getByText("失败子集")).toBeInTheDocument();
    expect(screen.getByText("102")).toBeInTheDocument();
    expect(screen.getByText(/INVALID_OUTPUT/)).toBeInTheDocument();
  });

  it("cancels a non-terminal run and keeps the terminal result visible", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "抽取运行 #88" });

    await user.click(screen.getByRole("button", { name: "取消运行" }));

    await waitFor(() => expect(mockedCancel).toHaveBeenCalledWith(88));
    expect(await screen.findByText("运行已取消")).toBeVisible();
    expect(screen.getByText("已取消")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "取消运行" }),
    ).not.toBeInTheDocument();
  });

  it("polls running jobs every three seconds and stops for terminal jobs", async () => {
    vi.useFakeTimers();
    try {
      renderPage();
      await vi.waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(3_000);
      await vi.waitFor(() =>
        expect(mockedGet.mock.calls.length).toBeGreaterThanOrEqual(2),
      );
      mockedGet.mockResolvedValue({ ...RUN, status: "completed" });
      await vi.advanceTimersByTimeAsync(3_000);
      await vi.waitFor(() =>
        expect(screen.getByText("运行成功")).toBeInTheDocument(),
      );
      const callsAfterTerminal = mockedGet.mock.calls.length;
      await vi.advanceTimersByTimeAsync(6_000);
      expect(mockedGet).toHaveBeenCalledTimes(callsAfterTerminal);
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries a failed run and exposes recoverable loading errors", async () => {
    const user = userEvent.setup();
    mockedGet.mockResolvedValueOnce({
      ...RUN,
      status: "failed",
      last_error_code: "MODEL_TIMEOUT",
      last_error_message: "模型超时",
    });
    renderPage();
    expect(await screen.findByText("模型超时")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试运行" }));
    await waitFor(() => expect(mockedRetry).toHaveBeenCalledWith(88));
    expect(await screen.findByText("重试任务已进入队列")).toBeVisible();
  });

  it("keeps manual retry enabled after attempt exhaustion and relabels it as a reset", async () => {
    const user = userEvent.setup();
    // 终态 failed 恰好在 attempt_count == max_attempts 时产生；服务端
    // retry_job 会把尝试归零，所以按钮必须保持可用。
    mockedGet.mockResolvedValueOnce({
      ...RUN,
      status: "failed",
      attempt_count: 3,
      max_attempts: 3,
      last_error_code: "MODEL_TIMEOUT",
      last_error_message: "模型超时",
    });
    renderPage();

    const retryButton = await screen.findByRole("button", {
      name: "重置尝试并重试",
    });
    expect(retryButton).toBeEnabled();
    await user.click(retryButton);
    await waitFor(() => expect(mockedRetry).toHaveBeenCalledWith(88));
    expect(await screen.findByText("重试任务已进入队列")).toBeVisible();
  });

  it("keeps retry available when a failed worker did not persist an error message", async () => {
    mockedGet.mockResolvedValueOnce({
      ...RUN,
      status: "failed",
      last_error_code: null,
      last_error_message: null,
    });
    renderPage();

    expect(
      await screen.findByRole("button", { name: "重试运行" }),
    ).toBeEnabled();
    expect(screen.getByText("运行发生错误")).toBeInTheDocument();
  });
});
